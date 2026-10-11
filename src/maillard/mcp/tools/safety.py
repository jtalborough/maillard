"""MCP tools for safety monitoring.

Provides tools to query safety status and limits during roasts.
"""

from __future__ import annotations

import logging
from typing import Any

from maillard.mcp.server import get_state_from_context, mcp

_logger = logging.getLogger(__name__)


@mcp.tool()
async def get_safety_status(ctx: Any) -> dict[str, Any]:
    """Get current safety monitor status and limits.

    Returns comprehensive safety information including:
    - Whether safety monitoring is active
    - Configured safety limits (temperature, duration, RoR)
    - Watchdog timer status
    - Active and recent safety alerts

    The safety monitor enforces limits during roasts and triggers
    emergency stops on critical violations. The watchdog timer
    detects if Claude stops making roast-participation tool calls.

    Returns:
        Dict with safety status:
        - active: Whether safety monitor is running
        - limits: Configured safety thresholds
        - watchdog: Watchdog timer state
        - active_alerts: Currently active alert conditions
        - recent_alerts: List of recent alerts
    """
    state = get_state_from_context(ctx)

    # If no safety monitor, return inactive status with config limits
    if state._safety_monitor is None:
        return {
            "active": False,
            "message": "Safety monitor not active. Starts when roast begins.",
            "limits": {
                "max_bt_temp": state.config.max_bt_temp,
                "max_et_temp": state.config.max_et_temp,
                "max_ror": state.config.max_ror,
                "min_ror": state.config.min_ror,
                "max_roast_duration": state.config.max_roast_duration,
            },
            "watchdog": {
                "warning_threshold": state.config.watchdog_warning_seconds,
                "emergency_threshold": state.config.watchdog_emergency_seconds,
            },
        }

    # Get full status from monitor
    return state._safety_monitor.get_status()


__all__ = ["get_safety_status"]
