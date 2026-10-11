"""MCP server for Maillard.

This module provides the FastMCP server instance and entry point for
the Maillard MCP server. The server exposes tools for:
- Coffee inventory management
- Roast profile management
- Roaster control and monitoring
- Roast analysis and recommendations

Usage:
    # Via CLI
    maillard mcp

    # Via Python
    from maillard.mcp.server import mcp
    mcp.run()

    # For Claude Desktop, configure in claude_desktop_config.json:
    {
        "mcpServers": {
            "maillard": {
                "command": "python",
                "args": ["-m", "maillard", "mcp"],
                "env": {
                    "MAILLARD_KALEIDO_HOST": "192.0.2.10"
                }
            }
        }
    }
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from maillard.core.config import get_config
from maillard.mcp.state import ApplicationState, get_app_state, lifespan

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def server_lifespan(_app: Any) -> AsyncIterator[dict[str, Any]]:
    """Lifespan handler for the MCP server.

    This handler initializes application state when the server starts
    and ensures proper cleanup when it shuts down.

    The state is stored in the context dict returned to FastMCP,
    making it available to tools via the Context parameter.

    Args:
        _app: The FastMCP application instance (unused)

    Yields:
        Context dict containing initialized application state
    """
    async with lifespan() as state:
        _logger.info("MCP server starting with state initialized")

        # Start embedded web dashboard if enabled
        web_task: asyncio.Task[None] | None = None
        uv_server: Any = None

        if state.config.web_enabled:
            try:
                import uvicorn

                from maillard.web import create_app

                web_app = create_app(state)
                uv_config = uvicorn.Config(
                    web_app,
                    host=state.config.api_host,
                    port=state.config.api_port,
                    log_level="warning",
                )
                # Don't let uvicorn touch the running event loop
                uv_config.setup_event_loop = lambda: None  # type: ignore[assignment]
                uv_server = uvicorn.Server(uv_config)
                web_task = asyncio.create_task(uv_server.serve())
                _logger.info(
                    "Web dashboard started at http://%s:%d",
                    state.config.api_host,
                    state.config.api_port,
                )
            except Exception:
                _logger.warning("Failed to start web dashboard", exc_info=True)

        try:
            yield {"state": state}
        finally:
            if uv_server is not None:
                uv_server.should_exit = True
            if web_task is not None:
                await web_task

    _logger.info("MCP server shutdown complete")


# Create the MCP server instance with lifespan handler
config = get_config()
mcp = FastMCP(
    name=config.mcp_server_name,
    instructions="""
Maillard is a coffee roasting assistant that will provide tools for:

1. **Manage Coffee Inventory**: Add, list, and track green coffee beans with
   their origin, processing method, altitude, and other characteristics.

2. **Create Roast Profiles**: Design profiles with specific temperature targets,
   heat/fan settings, and timing for each phase of the roast.

3. **Control the Roaster**: Connect to your Kaleido M10 roaster and control
   heat, fan, and drum settings in real-time.

4. **Monitor Roasts**: Track bean temperature, rate of rise, and roast progress
   during active roasts.

5. **Analyze Results**: Review completed roasts, add cupping notes, and get
   improvement suggestions.

IMPORTANT SAFETY NOTES:
- The roaster uses high temperatures that can cause burns or fire
- Always supervise roasts in person
- Critical actions (CHARGE, DROP) require explicit confirmation
- Emergency stop is always available and requires no confirmation

Currently available:
- `get_system_status` to check system and roaster status
- `add_coffee` to register new green coffee
- `list_coffees` to view coffee inventory
- `get_coffee` to get details about a specific coffee
- `update_coffee` to modify coffee information
- `delete_coffee` to remove coffee from inventory
- `list_profiles` to view saved roast profiles
- `get_profile` to get full profile details with phases
- `save_profile` to create a new roast profile
- `update_profile` to modify an existing profile
- `delete_profile` to remove a profile
- `generate_profile` to AI-generate a profile (coming soon)
- `connect_roaster` to connect to the Kaleido M10
- `disconnect_roaster` to safely disconnect
- `get_roaster_status` to check roaster state and temperatures
- `start_preheat` to begin preheating to charge temperature
- `execute_charge` to start the roast (CHARGE event) - requires confirmation
- `mark_turning_point` to mark the lowest BT after charge
- `mark_dry_end` to mark end of drying phase
- `mark_first_crack` to mark first crack start - requires confirmation
- `mark_first_crack_end` to mark first crack end
- `mark_second_crack` to mark second crack start
- `execute_drop` to end the roast (DROP event) - requires confirmation
- `set_heat` to set heating power (0-100%)
- `set_fan` to set fan speed (0-100%)
- `set_drum` to set drum rotation speed (0-100%)
- `set_target_temp` to set PID target temperature
- `toggle_pid` to enable/disable PID auto-heating mode
- `emergency_stop` to immediately shutdown (no confirmation needed)
""",
)


def get_state_from_context(ctx: Any) -> ApplicationState:
    """Extract ApplicationState from MCP context.

    Helper function for tools to get the application state
    from the FastMCP context.

    Args:
        ctx: FastMCP Context object

    Returns:
        The ApplicationState instance

    Raises:
        RuntimeError: If state is not available in context
    """
    # During lifespan, state is available in context
    # If accessed outside lifespan (e.g., testing), use global state
    if hasattr(ctx, "request_context") and ctx.request_context:
        lifespan_context = ctx.request_context.lifespan_context
        if lifespan_context and "state" in lifespan_context:
            return cast(ApplicationState, lifespan_context["state"])

    # Fallback to global state
    return get_app_state()


def _get_safe_database_info(
    mongodb_host: str, mongodb_port: int, mongodb_name: str
) -> dict[str, Any]:
    """Build safe database info from non-secret connection settings.

    Args:
        mongodb_host: MongoDB host
        mongodb_port: MongoDB port
        mongodb_name: MongoDB database name

    Returns:
        Dict with connection info (no credentials)
    """
    return {
        "backend": "mongodb",
        "host": mongodb_host,
        "port": mongodb_port,
        "database": mongodb_name,
        "configured": True,
    }


# =============================================================================
# System Tools (available immediately)
# =============================================================================


@mcp.tool()
async def get_system_status(ctx: Any) -> dict[str, Any]:
    """Get the current status of the Maillard system.

    Returns information about:
    - Database connection status
    - Roaster connection status
    - Active roast status
    - System configuration (safety limits only, no credentials)

    Returns:
        Dict with system status information
    """
    state = get_state_from_context(ctx)

    roaster_status = "not_connected"
    roaster_state: dict[str, Any] = {}

    if state.driver is not None:
        if state.driver.is_connected:
            roaster_status = "connected"
            roaster_state = dict(state.driver.state)
        else:
            roaster_status = "disconnected"

    return {
        "initialized": state.is_initialized,
        "roaster": {
            "status": roaster_status,
            "host": state.config.kaleido_host,
            "port": state.config.kaleido_port,
            "state": roaster_state,
        },
        "roast": {
            "active": state.roast_context.is_active,
            "roast_id": state.roast_context.roast_id,
            "profile_id": state.roast_context.profile_id,
            "coffee_id": state.roast_context.coffee_id,
        },
        "config": {
            "database": _get_safe_database_info(
                state.config.mongodb_host,
                state.config.mongodb_port,
                state.config.mongodb_name,
            ),
            "max_bt_temp": state.config.max_bt_temp,
            "max_et_temp": state.config.max_et_temp,
            "max_roast_duration": state.config.max_roast_duration,
        },
    }


# =============================================================================
# Tool imports - importing these modules registers the tools with the mcp server
# =============================================================================
import maillard.mcp.tools.coffee  # noqa: F401 - imports register @mcp.tool decorators
import maillard.mcp.tools.profile  # noqa: F401 - imports register @mcp.tool decorators
import maillard.mcp.tools.roaster  # noqa: F401 - imports register @mcp.tool decorators

import maillard.mcp.tools.monitoring  # noqa: F401 - imports register @mcp.tool decorators
import maillard.mcp.tools.safety  # noqa: F401 - imports register @mcp.tool decorators
import maillard.mcp.tools.execution  # noqa: F401 - imports register @mcp.tool decorators
import maillard.mcp.tools.cupping  # noqa: F401 - imports register @mcp.tool decorators

# Future tool modules (uncomment as implemented):
# import maillard.mcp.tools.analysis  # noqa: F401


if __name__ == "__main__":
    mcp.run()
