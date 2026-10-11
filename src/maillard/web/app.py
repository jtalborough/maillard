"""FastAPI application factory for the Maillard web dashboard."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from maillard.core.config import get_config
from maillard.web.public_routes import create_public_router
from maillard.web.routes import create_router
from maillard.web.websocket import websocket_endpoint

if TYPE_CHECKING:
    import anthropic

    from maillard.mcp.state import ApplicationState

_STATIC_DIR = Path(__file__).parent / "static"
_logger = logging.getLogger(__name__)


def _create_ai_client(api_key: str) -> anthropic.AsyncAnthropic:
    """Select authentication only; models come from Maillard settings."""
    import anthropic

    if api_key.startswith("sk-ant-oat01-"):
        # Empty api_key prevents the SDK from reloading the OAuth token from env.
        return anthropic.AsyncAnthropic(
            api_key="",
            auth_token=api_key,
            default_headers={
                "anthropic-beta": "oauth-2025-04-20",
                "X-Api-Key": anthropic.omit,
            },
        )
    return anthropic.AsyncAnthropic(api_key=api_key)


def create_app(state: ApplicationState) -> FastAPI:
    """Create the FastAPI application with shared state.

    Args:
        state: ApplicationState shared with the MCP server process.

    Returns:
        Configured FastAPI app.
    """
    from maillard.sync.site import SiteSyncClient, SiteSyncWorker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = app.state.site_sync_client
        worker = SiteSyncWorker(client)
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await client.aclose()

    app = FastAPI(title="Maillard Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.site_sync_client = SiteSyncClient(state.config)

    # Store state for route access
    app.state.maillard = state  # type: ignore[attr-defined]

    # --- Global exception handlers ---

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "Validation error", "detail": exc.errors()},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        _request: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        _logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )

    # Initialize AI agent.
    # Accepts ANTHROPIC_API_KEY (pay-per-token) or a Claude Max/Pro
    # setup-token (sk-ant-oat01-…) which bills against subscription.
    # Generate one with: claude setup-token
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            from maillard.ai.agent import RoastAIAgent

            is_sub = api_key.startswith("sk-ant-oat01-")
            model = state.config.ai_live_model
            client = _create_ai_client(api_key)
            ai_agent = RoastAIAgent(state, client, model=model)
            app.state.ai_agent = ai_agent  # type: ignore[attr-defined]
            _logger.info(
                "AI agent initialized (%s, %s)",
                model,
                "Max/Pro subscription" if is_sub else "API key",
            )
        except ImportError:
            _logger.warning("anthropic package not installed — AI agent disabled")
            app.state.ai_agent = None  # type: ignore[attr-defined]
    else:
        app.state.ai_agent = None  # type: ignore[attr-defined]
        _logger.info("No ANTHROPIC_API_KEY — AI agent disabled (run `claude setup-token` for Max/Pro)")

    # CORS — allow all private/LAN IPs by default (home appliance),
    # or restrict via MAILLARD_CORS_ORIGINS env var.
    cfg = get_config()
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in cfg.cors_origins.split(",")],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        # Default: localhost + all RFC-1918 private ranges
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=(
                r"http://(localhost|127\.0\.0\.1"
                r"|192\.168\.\d{1,3}\.\d{1,3}"
                r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
                r"(:\d+)?"
            ),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Version endpoint — before static mount so catch-all doesn't swallow it
    @app.get("/api/version")
    async def version() -> JSONResponse:
        from maillard.__version__ import BUILD_DATE, GIT_SHA, VERSION

        return JSONResponse(
            {"version": VERSION, "git_sha": GIT_SHA, "build_date": BUILD_DATE}
        )

    # Health check — before static mount so catch-all doesn't swallow it
    @app.get("/health")
    async def health() -> JSONResponse:
        from maillard.db import get_client

        mongo_ok = False
        client = get_client()
        if client is not None:
            try:
                await client.admin.command("ping")
                mongo_ok = True
            except Exception as exc:
                _logger.warning(
                    "MongoDB health check failed (error_type=%s)", type(exc).__name__
                )

        # Roaster state: not_configured / disconnected / connected
        if state.driver is None:
            roaster_status = "not_configured"
        elif state.driver.is_connected:
            roaster_status = "connected"
        else:
            roaster_status = "disconnected"

        status_code = 200 if mongo_ok else 503
        return JSONResponse(
            {
                "status": "healthy" if mongo_ok else "degraded",
                "mongodb": "connected" if mongo_ok else "disconnected",
                "roaster": roaster_status,
            },
            status_code=status_code,
        )

    # REST routes
    app.include_router(create_router())

    # Public routes (cupping form) — before static mount so /cup/{id} is matched
    app.include_router(create_public_router())

    # WebSocket
    app.add_api_websocket_route("/ws", websocket_endpoint)

    # Static files (dashboard HTML) — mounted last so API routes take priority
    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app
