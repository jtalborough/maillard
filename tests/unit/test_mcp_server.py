"""Unit tests for MCP server setup and initialization."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from maillard.core.config import get_config
from maillard.mcp import (
    ApplicationState,
    DriverType,
    RoastContext,
    get_app_state,
    lifespan,
    mcp,
    reset_app_state,
)
from maillard.mcp.server import _get_safe_database_info, get_state_from_context, get_system_status


# Note: reset_global_state fixture from conftest.py is applied automatically


def _make_mock_context(state: ApplicationState | None = None) -> Any:
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    if state is not None:
        ctx.request_context.lifespan_context = {"state": state}
    else:
        ctx.request_context = None
    return ctx


class TestApplicationState:
    """Tests for ApplicationState class."""

    def test_default_initialization(self) -> None:
        """Test ApplicationState with default values."""
        state = ApplicationState()

        assert state.config is not None
        assert state.driver is None
        assert state.event_bus is not None
        assert state.roast_context is not None
        assert not state.is_initialized
        assert state._driver_type == "kaleido"

    def test_is_initialized_property(self) -> None:
        """Test is_initialized property reflects internal state."""
        state = ApplicationState()
        assert not state.is_initialized

        state._initialized = True
        assert state.is_initialized

    def test_roast_context_defaults(self) -> None:
        """Test RoastContext default values."""
        context = RoastContext()

        assert context.roast_id is None
        assert context.profile_id is None
        assert context.coffee_id is None
        assert not context.is_active
        assert context.charge_time is None

    def test_roast_context_with_values(self) -> None:
        """Test RoastContext with values."""
        context = RoastContext(
            roast_id="roast-123",
            profile_id="profile-456",
            coffee_id="coffee-789",
            is_active=True,
            charge_time=1234567890.0,
        )

        assert context.roast_id == "roast-123"
        assert context.profile_id == "profile-456"
        assert context.coffee_id == "coffee-789"
        assert context.is_active
        assert context.charge_time == 1234567890.0

    def test_clear_roast_context(self) -> None:
        """Test clearing roast context."""
        state = ApplicationState()
        state.roast_context = RoastContext(
            roast_id="roast-123",
            is_active=True,
        )

        state.clear_roast_context()

        assert state.roast_context.roast_id is None
        assert not state.roast_context.is_active

    def test_create_driver_kaleido(self) -> None:
        """Test creating Kaleido driver."""
        state = ApplicationState()
        driver = state.create_driver("kaleido")

        assert driver is not None
        # KaleidoDriver starts disconnected
        assert not driver.is_connected

    def test_create_driver_simulator(self) -> None:
        """Test creating Simulator driver."""
        state = ApplicationState()
        driver = state.create_driver("simulator")

        assert driver is not None
        assert not driver.is_connected

    def test_create_driver_default(self) -> None:
        """Test creating driver with default type."""
        state = ApplicationState()
        state._driver_type = "simulator"
        driver = state.create_driver()

        assert driver is not None

    def test_create_driver_invalid_type(self) -> None:
        """Test creating driver with invalid type raises ValueError."""
        state = ApplicationState()

        with pytest.raises(ValueError, match="Unknown driver type"):
            state.create_driver("invalid")  # type: ignore[arg-type]


class TestLifespan:
    """Tests for lifespan management."""

    async def test_lifespan_initializes_state(self, mongo_db) -> None:
        """Test that lifespan initializes state."""
        async with lifespan() as state:
            assert state.is_initialized
            assert state.driver is None  # Not auto-connected

    async def test_lifespan_cleanup(self, mongo_db) -> None:
        """Test that lifespan cleans up state."""
        async with lifespan() as state:
            pass

        # After context exits, state should be shutdown
        # Note: The global state is cleaned up in fixture

    async def test_get_app_state_returns_same_instance(self, mongo_db) -> None:
        """Test that get_app_state returns singleton."""
        state1 = get_app_state()
        state2 = get_app_state()

        assert state1 is state2

    async def test_reset_app_state_clears_singleton(self, mongo_db) -> None:
        """Test that reset_app_state clears the singleton."""
        state1 = get_app_state()
        reset_app_state()
        state2 = get_app_state()

        assert state1 is not state2


class TestGetStateFromContext:
    """Tests for get_state_from_context helper."""

    def test_get_state_from_context_with_lifespan_context(self) -> None:
        """Test extracting state from MCP context."""
        expected_state = ApplicationState()
        ctx = _make_mock_context(expected_state)

        result = get_state_from_context(ctx)

        assert result is expected_state

    def test_get_state_from_context_fallback_to_global(self) -> None:
        """Test fallback to global state when context unavailable."""
        ctx = _make_mock_context(None)

        result = get_state_from_context(ctx)

        assert result is get_app_state()


class TestMCPServer:
    """Tests for MCP server instance."""

    def test_mcp_server_name(self) -> None:
        """Test MCP server has correct name."""
        config = get_config()
        assert mcp.name == config.mcp_server_name

    def test_mcp_server_has_instructions(self) -> None:
        """Test MCP server has instructions defined."""
        # Instructions should include key functionality
        assert mcp.instructions is not None
        instructions = mcp.instructions
        assert "coffee" in instructions.lower()
        assert "roaster" in instructions.lower()

    async def test_get_system_status_uninitialized(self, mongo_db) -> None:
        """Test get_system_status when not initialized."""
        ctx = _make_mock_context(None)
        status = await get_system_status(ctx)

        assert status["initialized"] is False
        assert status["roaster"]["status"] == "not_connected"
        assert status["roast"]["active"] is False

    async def test_get_system_status_initialized(self, mongo_db) -> None:
        """Test get_system_status after initialization."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            status = await get_system_status(ctx)

            assert status["initialized"] is True
            assert status["roaster"]["status"] == "not_connected"
            assert "host" in status["roaster"]
            assert "port" in status["roaster"]

    async def test_get_system_status_config_values(self, mongo_db) -> None:
        """Test get_system_status returns config values."""
        config = get_config()
        ctx = _make_mock_context(None)
        status = await get_system_status(ctx)

        assert status["config"]["max_bt_temp"] == config.max_bt_temp
        assert status["config"]["max_et_temp"] == config.max_et_temp
        assert status["config"]["max_roast_duration"] == config.max_roast_duration

    async def test_get_system_status_database_info_redacted(self, mongo_db) -> None:
        """Test get_system_status does not expose full database URL."""
        ctx = _make_mock_context(None)
        status = await get_system_status(ctx)

        # Should have database info dict, not raw URL
        assert "database" in status["config"]
        db_info = status["config"]["database"]
        assert isinstance(db_info, dict)
        assert "configured" in db_info
        # Should not contain the full URL string
        assert "database_url" not in status["config"]


class TestSafeDatabaseInfo:
    """Tests for _get_safe_database_info helper."""

    def test_mongodb_url_basic(self) -> None:
        """Test database status from non-secret connection settings."""
        info = _get_safe_database_info("localhost", 27017, "maillard")

        assert info["backend"] == "mongodb"
        assert info["host"] == "localhost"
        assert info["port"] == 27017
        assert info["database"] == "maillard"
        assert info["configured"] is True

    def test_database_info_contains_only_safe_fields(self) -> None:
        """Test database status contains only non-secret settings."""
        info = _get_safe_database_info("dbhost.example.com", 27017, "mydb")

        assert info["backend"] == "mongodb"
        assert info["host"] == "dbhost.example.com"
        assert info["database"] == "mydb"
        assert info["configured"] is True
        assert set(info) == {"backend", "host", "port", "database", "configured"}


class TestDriverConnection:
    """Tests for driver connection through ApplicationState."""

    async def test_connect_roaster_simulator(self, mongo_db) -> None:
        """Test connecting to simulator driver."""
        state = ApplicationState()
        await state.initialize()

        try:
            await state.connect_roaster(driver_type="simulator", host="127.0.0.1", port=80)

            assert state.driver is not None
            assert state.driver.is_connected
        finally:
            await state.shutdown()

    async def test_connect_roaster_already_connected(self, mongo_db) -> None:
        """Test that connecting when already connected raises error."""
        state = ApplicationState()
        await state.initialize()

        try:
            await state.connect_roaster(driver_type="simulator")

            with pytest.raises(RuntimeError, match="Already connected"):
                await state.connect_roaster(driver_type="simulator")
        finally:
            await state.shutdown()

    async def test_disconnect_roaster(self, mongo_db) -> None:
        """Test disconnecting from roaster."""
        state = ApplicationState()
        await state.initialize()

        try:
            await state.connect_roaster(driver_type="simulator")
            await state.disconnect_roaster()

            assert state.driver is None
        finally:
            await state.shutdown()

    async def test_disconnect_roaster_not_connected(self, mongo_db) -> None:
        """Test disconnecting when not connected is a no-op."""
        state = ApplicationState()
        await state.initialize()

        try:
            # Should not raise
            await state.disconnect_roaster()
            assert state.driver is None
        finally:
            await state.shutdown()


class TestDriverType:
    """Tests for DriverType literal type."""

    def test_driver_type_values(self) -> None:
        """Test DriverType accepts valid values."""
        # These should not raise
        dtype1: DriverType = "kaleido"
        dtype2: DriverType = "simulator"
        assert dtype1 == "kaleido"
        assert dtype2 == "simulator"
