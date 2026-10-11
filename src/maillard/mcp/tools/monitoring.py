"""MCP tools for roast monitoring.

Provides real-time roast progress data including temperatures, RoR,
phase information, and event history.
"""

from __future__ import annotations

import logging
from typing import Any

from maillard.mcp.server import get_state_from_context, mcp

_logger = logging.getLogger(__name__)


def _reset_safety_watchdog(state: Any) -> None:
    """Reset the safety monitor watchdog timer if active."""
    if state._safety_monitor is not None and state._safety_monitor.is_running:
        state._safety_monitor.reset_watchdog()


@mcp.tool()
async def get_roast_progress(ctx: Any) -> dict[str, Any]:
    """Get real-time roast progress data.

    Returns comprehensive roast data including current temperatures,
    rate of rise, elapsed time, phase, sample count, and events.

    Requires an active roast session (started via execute_charge).

    Returns:
        Dict with roast progress data:
        - bt: Current bean temperature
        - et: Current environment temperature
        - ror_instant: Instantaneous rate of rise (°C/min)
        - ror_smoothed: 30-second smoothed rate of rise (°C/min)
        - elapsed_seconds: Time since charge in seconds
        - current_phase: Current roast phase name
        - sample_count: Number of recorded samples
        - events: List of marked roast events
        - monitor_running: Whether background monitoring is active
    """
    state = get_state_from_context(ctx)

    if state.roast_session is None or not state.roast_session.is_active:
        return {
            "status": "no_active_roast",
            "message": "No active roast session. Use execute_charge to start a roast.",
        }

    # Reset safety watchdog - proves Claude is actively monitoring
    _reset_safety_watchdog(state)

    session = state.roast_session

    # Current temperatures from driver
    bt: float | None = None
    et: float | None = None
    if state.driver is not None and state.driver.is_connected:
        driver_state = state.driver.state
        bt = driver_state.get("BT")
        et = driver_state.get("ET")

    # RoR values
    ror_instant = session.latest_ror
    ror_smoothed: float | None = None
    ror_list = session.collector.calculate_ror(window_seconds=30)
    if ror_list:
        ror_smoothed = ror_list[-1]

    # Elapsed time
    elapsed = session.elapsed_time

    # Phase
    phase = session.current_phase

    # Events from collector
    events = [
        {
            "type": e.type,
            "time_seconds": e.time_seconds,
            "temp": e.temp,
        }
        for e in session.collector.events
    ]

    return {
        "status": "active",
        "bt": bt,
        "et": et,
        "ror_instant": round(ror_instant, 1) if ror_instant is not None else None,
        "ror_smoothed": round(ror_smoothed, 1) if ror_smoothed is not None else None,
        "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
        "current_phase": phase.name if phase is not None else None,
        "sample_count": session.sample_count,
        "events": events,
        "monitor_running": state._monitor is not None and state._monitor.is_running,
    }


__all__ = ["get_roast_progress"]
