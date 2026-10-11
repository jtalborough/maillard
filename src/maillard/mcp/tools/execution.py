"""MCP tools for roast execution and profile following.

Provides tools to start roasts with profiles, get execution status, and
switch control modes between PROFILE (auto-follow), MANUAL (Claude controls),
and HOLD (maintain current settings).

These tools integrate with the RoastExecutionEngine which autonomously follows
roast profiles at 1.5s intervals, decoupling tight control loops from Claude's
response latency.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from maillard.core.safety import SafetyMonitor
from maillard.db.models import Roast, RoastStatus
from maillard.db.repository import ProfileRepository
from maillard.db.schemas import PhaseSchema
from maillard.driver.base import RoastEvent
from maillard.mcp.monitoring import RoastMonitor
from maillard.mcp.server import get_state_from_context, mcp
from maillard.mcp.state import ApplicationState
from maillard.roasting.execution import ControlMode, RoastExecutionEngine
from maillard.roasting.session import RoastSession

_logger = logging.getLogger(__name__)


def _reset_safety_watchdog(state: ApplicationState) -> None:
    """Reset the safety monitor watchdog timer if active."""
    if state._safety_monitor is not None and state._safety_monitor.is_running:
        state._safety_monitor.reset_watchdog()


# =============================================================================
# Execution Tools
# =============================================================================


@mcp.tool()
async def start_roast(
    ctx: Any,
    coffee_id: str | None = None,
    profile_id: str | None = None,
    batch_size_grams: int | None = None,
    confirmed: bool = False,
    drop_id: str | None = None,
) -> dict[str, Any]:
    """Start a roast with optional profile for autonomous execution.

    REQUIRES HUMAN CONFIRMATION. This tool:
    1. Creates a DB roast record
    2. Preheats to the profile's charge temperature (if profile specified)
    3. Waits for charge confirmation
    4. Starts the execution engine to follow the profile

    If no profile is specified, the roast starts in MANUAL mode where
    Claude controls all settings.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: ID of the coffee being roasted
        profile_id: ID of the profile to follow (optional, enables auto-execution)
        batch_size_grams: Batch size in grams
        confirmed: Must be True to execute the charge

    Returns:
        Confirmation request if not confirmed, or roast start confirmation
    """
    state = get_state_from_context(ctx)

    # Check connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected. Use connect_roaster first.")

    # Check if roast already active (allow charge during preheat/ready states)
    if state.is_roast_active:
        session = state.roast_session
        if session is not None and session.machine.current_state not in (
            session.machine.preheating,
            session.machine.ready_to_charge,
        ):
            return {
                "status": "error",
                "error": (
                    f"Roast already active (id: {state.roast_context.roast_id}). "
                    "Complete or abort current roast first."
                ),
            }

    # Load profile if specified
    profile_phases: list[PhaseSchema] = []
    charge_temp: float | None = None
    profile_name: str | None = None

    if profile_id:
        profile_repo = ProfileRepository()
        profile = await profile_repo.get_by_id(profile_id)
        if profile is None:
            return {
                "status": "error",
                "error": f"Profile not found: {profile_id}",
            }
        profile_name = profile.name
        charge_temp = profile.charge_temp

        # Phases are native subdocuments (list of dicts)
        try:
            profile_phases = [PhaseSchema(**p) for p in (profile.phases or [])]
        except Exception as e:
            return {
                "status": "error",
                "error": f"Invalid profile phases: {e}",
            }

    # Get current state for confirmation message
    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)

    if not confirmed:
        msg_parts = [f"Ready to start roast at {current_bt:.1f}°C BT."]
        if profile_name:
            msg_parts.append(f"Profile: {profile_name}")
            if charge_temp:
                msg_parts.append(f"(charge temp: {charge_temp:.1f}°C)")
            msg_parts.append("Execution engine will auto-follow profile phases.")
        else:
            msg_parts.append("No profile - roast will start in MANUAL mode.")
        msg_parts.append("Call start_roast with confirmed=True to proceed.")

        return {
            "requires_confirmation": True,
            "action": "start_roast",
            "message": " ".join(msg_parts),
            "current_bt": current_bt,
            "coffee_id": coffee_id,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "charge_temp": charge_temp,
            "has_profile": bool(profile_phases),
        }

    if drop_id:
        from maillard.db.models import PresaleDrop
        drop = await PresaleDrop.get(drop_id)
        if drop is None or drop.coffee_id != coffee_id or drop.status == "cancelled":
            raise ValueError("Select a valid drop for the charged coffee")

    # Execute charge on hardware
    await state.driver.trigger_event(RoastEvent.CHARGE)

    # Create DB record
    charge_time = datetime.now(UTC)
    roast_record = Roast(
        profile_id=profile_id,
        coffee_id=coffee_id,
        batch_size_grams=batch_size_grams,
        green_weight_grams=float(batch_size_grams) if batch_size_grams is not None else None,
        drop_id=drop_id,
        start_time=charge_time.isoformat(),
        charge_temp=current_bt,
        status=RoastStatus.IN_PROGRESS,
    )
    await roast_record.insert()
    roast_id = roast_record.id

    # Create RoastSession
    existing_machine = None
    if state.roast_session is not None:
        existing_machine = state.roast_session.machine

    session = RoastSession.start(
        event_bus=state.event_bus,
        charge_temp=current_bt,
        roast_id=roast_id,
        coffee_id=coffee_id,
        profile_id=profile_id,
        batch_size_grams=batch_size_grams,
        machine=existing_machine,
    )
    state.roast_session = session

    # Update legacy roast context
    state.roast_context.is_active = True
    state.roast_context.charge_time = charge_time.timestamp()
    state.roast_context.coffee_id = coffee_id
    state.roast_context.profile_id = profile_id
    state.roast_context.roast_id = roast_id

    # Start background monitoring
    monitor = RoastMonitor(
        driver=state.driver,
        event_bus=state.event_bus,
        session=session,
        poll_interval=state.config.poll_interval,
        stale_alarm_seconds=state.config.stale_alarm_seconds,
        alarm_clear_seconds=state.config.alarm_clear_seconds,
    )
    await monitor.start()
    state._monitor = monitor

    # Start safety monitoring
    safety_monitor = SafetyMonitor(
        driver=state.driver,
        event_bus=state.event_bus,
        session=session,
        config=state.config,
        on_emergency=state.halt_heat_sources,
    )
    await safety_monitor.start()
    state._safety_monitor = safety_monitor

    # Start execution engine if profile provided
    execution_mode = ControlMode.MANUAL
    if profile_phases:
        execution_engine = RoastExecutionEngine(
            driver=state.driver,
            event_bus=state.event_bus,
            session=session,
            profile_phases=profile_phases,
            initial_mode=ControlMode.PROFILE,
        )
        await execution_engine.start()
        state._execution_engine = execution_engine
        execution_mode = ControlMode.PROFILE

    _logger.info(
        "Roast started at %.1f°C (roast_id=%s, mode=%s)",
        current_bt,
        roast_id,
        execution_mode.value,
    )

    return {
        "status": "started",
        "message": f"Roast started at {current_bt:.1f}°C. Execution mode: {execution_mode.value}",
        "charge_temp": current_bt,
        "charge_time": charge_time.isoformat(),
        "roast_id": roast_id,
        "coffee_id": coffee_id,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "batch_size_grams": batch_size_grams,
        "execution_mode": execution_mode.value,
        "phase_count": len(profile_phases) if profile_phases else 0,
    }


@mcp.tool()
async def get_execution_status(ctx: Any) -> dict[str, Any]:
    """Get comprehensive status of the roast execution engine.

    Returns information about:
    - Control mode (PROFILE, MANUAL, HOLD)
    - Current and next profile phases
    - Target vs actual values
    - Suggested events pending confirmation
    - Phase progress and time estimates

    Use this tool to understand what the execution engine is doing and
    whether any events need to be confirmed.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict with execution status, phase info, and suggestions
    """
    state = get_state_from_context(ctx)

    _reset_safety_watchdog(state)

    # Base response
    response: dict[str, Any] = {
        "roast_active": state.is_roast_active,
        "roast_id": state.roast_context.roast_id,
    }

    # Check if execution engine exists
    if state._execution_engine is None:
        response["execution_engine"] = None
        response["message"] = "No execution engine active"
        return response

    # Get engine status
    engine_status = state._execution_engine.get_status()
    response["execution_engine"] = engine_status

    # Add current roaster state
    if state.driver and state.driver.is_connected:
        driver_state = state.driver.state
        response["current_state"] = {
            "bt": driver_state.get("BT"),
            "et": driver_state.get("ET"),
            "heat_percent": driver_state.get("HP"),
            "fan_percent": driver_state.get("FC"),
            "drum_percent": driver_state.get("RC"),
        }

    # Add session info
    if state.roast_session:
        response["session"] = {
            "state": state.roast_session.state_enum.value,
            "phase": state.roast_session.current_phase.name if state.roast_session.current_phase else None,
            "elapsed_seconds": round(state.roast_session.elapsed_time or 0, 1),
            "sample_count": state.roast_session.sample_count,
            "latest_ror": round(state.roast_session.latest_ror, 1) if state.roast_session.latest_ror else None,
        }

    # Provide guidance based on suggested events
    suggested = engine_status.get("suggested_events", [])
    if "drop" in suggested:
        response["action_needed"] = "Profile target reached - confirm drop when ready"
    elif "fc_start" in suggested:
        response["action_needed"] = "Approaching first crack temperature - listen for cracks"
    elif "dry_end" in suggested:
        response["action_needed"] = "Approaching dry end - observe color/smell changes"
    elif "turning_point" in suggested:
        response["action_needed"] = "Turning point detected - confirm to mark"

    return response


@mcp.tool()
async def switch_control_mode(ctx: Any, mode: str) -> dict[str, Any]:
    """Switch the execution engine control mode.

    Available modes:
    - "profile": Auto-follow profile phases (heat, fan, drum interpolation)
    - "manual": Claude controls all settings; engine monitors and suggests events
    - "hold": Maintain current settings; engine monitors and suggests events

    Use "manual" when you want to make adjustments based on sensory feedback.
    Use "hold" to freeze current settings temporarily.
    Use "profile" to resume automatic profile following.

    Args:
        ctx: MCP context (injected automatically)
        mode: Target mode ("profile", "manual", or "hold")

    Returns:
        Dict confirming mode switch
    """
    state = get_state_from_context(ctx)

    _reset_safety_watchdog(state)

    # Validate mode
    mode_lower = mode.lower()
    try:
        new_mode = ControlMode(mode_lower)
    except ValueError:
        valid_modes = [m.value for m in ControlMode]
        return {
            "status": "error",
            "error": f"Invalid mode: {mode}. Must be one of: {valid_modes}",
        }

    # Check if execution engine exists
    if state._execution_engine is None:
        return {
            "status": "error",
            "error": "No execution engine active. Start a roast with a profile first.",
        }

    # Switch mode
    old_mode = state._execution_engine.mode
    state._execution_engine.switch_mode(new_mode)

    _logger.info("Control mode switched: %s -> %s", old_mode.value, new_mode.value)

    # Provide context about what the mode change means
    mode_descriptions = {
        ControlMode.PROFILE: "Engine will auto-adjust heat/fan/drum based on profile phases",
        ControlMode.MANUAL: "You have full control. Engine will suggest events but not adjust settings",
        ControlMode.HOLD: "Current settings frozen. Engine will suggest events but not adjust settings",
    }

    return {
        "status": "switched",
        "previous_mode": old_mode.value,
        "new_mode": new_mode.value,
        "message": mode_descriptions[new_mode],
    }


__all__ = [
    "get_execution_status",
    "start_roast",
    "switch_control_mode",
]
