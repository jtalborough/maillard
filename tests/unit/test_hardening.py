"""Tests for production hardening: retry logic, logging, exception handlers, health."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from maillard.core.config import MaillardConfig


# ---------------------------------------------------------------------------
# T-1: MongoDB retry with backoff
# ---------------------------------------------------------------------------


class TestInitDbRetry:
    """Tests for init_db() retry logic."""

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self) -> None:
        """init_beanie fails twice, then succeeds on third attempt."""
        import maillard.db as db_module

        call_count = 0

        async def flaky_init_beanie(**kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("MongoDB unavailable")

        with patch.object(db_module, "init_beanie", side_effect=flaky_init_beanie):
            with patch("maillard.db.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                db_module._client = None
                await db_module.init_db()

        assert call_count == 3
        assert db_module._client is not None
        # Verify backoff delays: 1s, 2s
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

        # cleanup
        db_module._client = None

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self) -> None:
        """init_db raises after exhausting all retries."""
        import maillard.db as db_module

        async def always_fail(**kwargs: object) -> None:
            raise ConnectionError("MongoDB unavailable")

        with patch.object(db_module, "init_beanie", side_effect=always_fail):
            with patch("maillard.db.asyncio.sleep", new_callable=AsyncMock):
                db_module._client = None
                with pytest.raises(RuntimeError, match="MongoDB initialization failed"):
                    await db_module.init_db()

        assert db_module._client is None

    @pytest.mark.asyncio
    async def test_idempotent_when_already_connected(self) -> None:
        """init_db is a no-op if _client is already set."""
        import maillard.db as db_module

        sentinel = MagicMock()
        db_module._client = sentinel

        await db_module.init_db()

        # Should not have been replaced
        assert db_module._client is sentinel
        db_module._client = None


# ---------------------------------------------------------------------------
# T-2: Centralized logging — JSON format
# ---------------------------------------------------------------------------


class TestJSONLogging:
    """Tests for JSON log format output."""

    def test_json_formatter_produces_valid_json(self) -> None:
        """_JSONFormatter output parses as valid JSON."""
        from maillard.__main__ import _JSONFormatter

        formatter = _JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message %s",
            args=("arg1",),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "Test message arg1"
        assert "timestamp" in parsed

    def test_json_formatter_includes_exception(self) -> None:
        """_JSONFormatter includes exception info when present."""
        from maillard.__main__ import _JSONFormatter

        formatter = _JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Boom",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]

    def test_log_format_config_default(self) -> None:
        """MaillardConfig defaults log_format to 'text'."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("MAILLARD_")}
        with patch.dict(os.environ, clean_env, clear=True):
            config = MaillardConfig(_env_file=None)
            assert config.log_format == "text"

    def test_log_format_config_json(self) -> None:
        """MaillardConfig reads MAILLARD_LOG_FORMAT=json."""
        with patch.dict(os.environ, {"MAILLARD_LOG_FORMAT": "json"}):
            config = MaillardConfig()
            assert config.log_format == "json"


# ---------------------------------------------------------------------------
# T-3: Global exception handlers
# ---------------------------------------------------------------------------


def _make_test_app() -> TestClient:
    """Create a TestClient with an app that has exception handlers + test routes."""
    from maillard.mcp.state import ApplicationState
    from maillard.web import app as app_module

    state = ApplicationState()
    state._initialized = True

    # Prevent static file mount from catching all routes
    with patch.object(app_module, "_STATIC_DIR", MagicMock(is_dir=MagicMock(return_value=False))):
        app = app_module.create_app(state)

    # Add test routes that trigger each error type
    @app.get("/test/500")
    async def trigger_500() -> None:
        raise RuntimeError("unexpected failure")

    @app.get("/test/404")
    async def trigger_404() -> None:
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/test/validation")
    async def trigger_validation(required_param: int) -> dict:
        return {"value": required_param}

    return TestClient(app, raise_server_exceptions=False)


class TestExceptionHandlers:
    """Tests for global exception handlers."""

    def test_unhandled_exception_returns_500_json(self) -> None:
        """Unhandled exceptions return JSON with no traceback."""
        client = _make_test_app()
        resp = client.get("/test/500")

        assert resp.status_code == 500
        body = resp.json()
        assert body == {"error": "Internal server error"}
        # No traceback leaked in body
        assert "RuntimeError" not in resp.text
        assert "Traceback" not in resp.text

    def test_http_exception_returns_correct_status(self) -> None:
        """HTTPException returns its status code and detail."""
        client = _make_test_app()
        resp = client.get("/test/404")

        assert resp.status_code == 404
        body = resp.json()
        assert body == {"error": "Not found"}

    def test_validation_error_returns_422(self) -> None:
        """Missing required param returns 422 with structured error."""
        client = _make_test_app()
        # Omit required_param to trigger validation error
        resp = client.get("/test/validation")

        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "Validation error"
        assert "detail" in body


# ---------------------------------------------------------------------------
# T-5: Enhanced /health with roaster state
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for the /health endpoint roaster state field."""

    def test_health_includes_roaster_not_configured(self) -> None:
        """When driver is None, roaster status is 'not_configured'."""
        from maillard.mcp.state import ApplicationState
        from maillard.web.app import create_app

        state = ApplicationState()
        state._initialized = True
        state.driver = None
        app = create_app(state)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/health")
        body = resp.json()
        assert body["roaster"] == "not_configured"

    def test_health_includes_roaster_disconnected(self) -> None:
        """When driver exists but not connected, roaster status is 'disconnected'."""
        from maillard.mcp.state import ApplicationState
        from maillard.web.app import create_app

        state = ApplicationState()
        state._initialized = True
        mock_driver = MagicMock()
        mock_driver.is_connected = False
        state.driver = mock_driver
        app = create_app(state)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/health")
        body = resp.json()
        assert body["roaster"] == "disconnected"

    def test_health_includes_roaster_connected(self) -> None:
        """When driver is connected, roaster status is 'connected'."""
        from maillard.mcp.state import ApplicationState
        from maillard.web.app import create_app

        state = ApplicationState()
        state._initialized = True
        mock_driver = MagicMock()
        mock_driver.is_connected = True
        state.driver = mock_driver
        app = create_app(state)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/health")
        body = resp.json()
        assert body["roaster"] == "connected"
