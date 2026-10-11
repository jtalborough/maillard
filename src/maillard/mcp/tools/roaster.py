"""MCP tools for roaster connection and control.

Provides tools to connect to the Kaleido M10 roaster, control heat/fan/drum,
trigger roast events, and monitor roaster status.

SAFETY NOTES:
- The roaster uses high temperatures that can cause burns or fire
- Always supervise roasts in person
- Critical actions (CHARGE, FIRST CRACK, DROP) require explicit confirmation
- Emergency stop is always available and requires no confirmation
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from statemachine.exceptions import TransitionNotAllowed

from maillard.core.heating import HeatingManager
from maillard.core.safety import SafetyMonitor
from maillard.db.models import Roast, RoastStatus
from maillard.driver.base import RoastEvent
from maillard.mcp.monitoring import RoastMonitor
from maillard.mcp.server import get_state_from_context, mcp
from maillard.mcp.state import ApplicationState, DriverType
from maillard.roasting.curve import CurveCollector
from maillard.roasting.events import derive_roast_event_fields
from maillard.roasting.execution import ControlMode
from maillard.roasting.session import RoastSession
from maillard.roasting.state import InvalidStateError

_logger = logging.getLogger(__name__)


def _reset_safety_watchdog(state: ApplicationState) -> None:
    """Reset the safety monitor watchdog timer if active.

    Call this in roast-participation tools to prove Claude is actively monitoring.
    """
    if state._safety_monitor is not None and state._safety_monitor.is_running:
        state._safety_monitor.reset_watchdog()


def _notify_manual_override(state: ApplicationState, control: str, value: int) -> None:
    """Notify execution engine of manual control override.

    If the execution engine is running in PROFILE mode, this will
    auto-switch it to MANUAL mode.

    Args:
        state: Application state
        control: Control name ("heat", "fan", "drum")
        value: New value
    """
    if (
        state._execution_engine is not None
        and state._execution_engine.is_running
        and state._execution_engine.mode == ControlMode.PROFILE
    ):
        state._execution_engine.notify_manual_override(control, value)


async def _start_heating_manager(state: ApplicationState, heat_percent: int) -> None:
    """Start or update the HeatingManager for reliable heat control.

    The Kaleido M10 requires HS=1 to be continuously re-sent for the heater
    to produce heat. This function manages the HeatingManager lifecycle.

    Args:
        state: Application state
        heat_percent: Heat power percentage (0-100)
    """
    if state.driver is None:
        return

    if heat_percent > 0:
        # Start or update heating manager
        if state._heating_manager is None:
            state._heating_manager = HeatingManager(state.driver, event_bus=state.event_bus)
        await state._heating_manager.start(heat_percent)
    else:
        # Stop heating manager when heat is 0
        await _stop_heating_manager(state)


async def _stop_heating_manager(state: ApplicationState) -> None:
    """Stop the HeatingManager if running.

    Args:
        state: Application state
    """
    if state._heating_manager is not None:
        await state._heating_manager.stop()
        state._heating_manager = None


def _get_elapsed_seconds(state: ApplicationState) -> float | None:
    """Get elapsed time since charge in seconds."""
    if state.roast_session is not None:
        return state.roast_session.elapsed_time
    if state.roast_context.charge_time is None:
        return None
    return datetime.now(UTC).timestamp() - state.roast_context.charge_time


async def _update_roast_event(
    state: ApplicationState,
    **fields: Any,
) -> None:
    """Update the Roast DB record with event fields.

    Args:
        state: Application state containing the active session
        **fields: Column names and values to update on the Roast record
    """
    session = state.roast_session
    if session is None or session.roast_id is None:
        return
    roast = await Roast.get(session.roast_id)
    if roast is not None:
        await roast.set(fields)


async def _get_roaster_state_summary(state: ApplicationState) -> dict[str, Any]:
    """Get a summary of roaster state for response.

    Actively polls the driver to get fresh state data.
    """
    if state.driver is None or not state.driver.is_connected:
        return {
            "connected": False,
            "bt": None,
            "et": None,
            "heat_percent": None,
            "fan_percent": None,
            "drum_percent": None,
            "target_temp": None,
            "pid_enabled": None,
        }

    # Actively poll for fresh state (required for Kaleido driver)
    driver_state = await state.driver.read_state()
    return {
        "connected": True,
        "bt": driver_state.get("BT"),
        "et": driver_state.get("ET"),
        "heat_percent": driver_state.get("HP"),
        "heating_enabled": driver_state.get("HS") == 1,
        "fan_percent": driver_state.get("FC"),
        "drum_percent": driver_state.get("RC"),
        "target_temp": driver_state.get("TS"),
        "pid_enabled": driver_state.get("AH") == 1,
    }


# =============================================================================
# Connection Tools
# =============================================================================


@mcp.tool()
async def get_roaster_status(ctx: Any) -> dict[str, Any]:
    """Get current roaster state including connection status and temperatures.

    Returns comprehensive information about the roaster's current state:
    - Connection status (connected/disconnected)
    - Current temperatures (BT, ET)
    - Control settings (heat, fan, drum percentages)
    - PID mode status
    - Active roast information

    Returns:
        Dict with roaster status information
    """
    state = get_state_from_context(ctx)

    roaster_summary = await _get_roaster_state_summary(state)

    # Add roast context
    session = state.roast_session
    roast_info: dict[str, Any] = {
        "active": state.is_roast_active,
        "roast_id": state.roast_context.roast_id,
        "profile_id": state.roast_context.profile_id,
        "coffee_id": state.roast_context.coffee_id,
    }

    if session is not None and session.is_active:
        elapsed = session.elapsed_time
        roast_info["elapsed_seconds"] = int(elapsed) if elapsed else None
        roast_info["state"] = session.state_enum.value
        phase = session.current_phase
        roast_info["phase"] = phase.name if phase else None
        roast_info["latest_ror"] = session.latest_ror
        roast_info["sample_count"] = session.sample_count
    elif state.roast_context.is_active and state.roast_context.charge_time:
        elapsed = _get_elapsed_seconds(state)
        roast_info["elapsed_seconds"] = int(elapsed) if elapsed else None

    return {
        "roaster": roaster_summary,
        "roast": roast_info,
        "config": {
            "host": state.config.kaleido_host,
            "port": state.config.kaleido_port,
        },
    }


@mcp.tool()
async def connect_roaster(
    ctx: Any,
    host: str | None = None,
    port: int | None = None,
    driver_type: str | None = None,
) -> dict[str, Any]:
    """Connect to the Kaleido M10 roaster via WebSocket.

    Establishes a connection to the roaster and performs the safety handshake.
    If already connected, returns the current connection status.

    Args:
        ctx: MCP context (injected automatically)
        host: Roaster IP address (defaults to config value, typically 192.0.2.10)
        port: Roaster port (defaults to config value, typically 80)
        driver_type: Driver type - "kaleido" for real hardware or "simulator" for testing

    Returns:
        Dict with connection status and roaster info
    """
    state = get_state_from_context(ctx)

    # Check if already connected
    if state.driver is not None and state.driver.is_connected:
        return {
            "status": "already_connected",
            "host": state.config.kaleido_host,
            "port": state.config.kaleido_port,
            "roaster": await _get_roaster_state_summary(state),
        }

    # Validate driver type if provided
    dtype: DriverType | None = None
    if driver_type is not None:
        if driver_type not in ("kaleido", "simulator"):
            raise ValueError(f"Invalid driver_type: {driver_type}. Must be 'kaleido' or 'simulator'")
        dtype = driver_type  # type: ignore[assignment]

    # Connect
    connect_host = host or state.config.kaleido_host
    connect_port = port or state.config.kaleido_port

    try:
        await state.connect_roaster(driver_type=dtype, host=connect_host, port=connect_port)
        _logger.info("Connected to roaster at %s:%d", connect_host, connect_port)

        return {
            "status": "connected",
            "host": connect_host,
            "port": connect_port,
            "roaster": await _get_roaster_state_summary(state),
        }
    except TimeoutError as e:
        _logger.error("Connection timeout to %s:%d", connect_host, connect_port)
        raise TimeoutError(f"Connection timeout to roaster at {connect_host}:{connect_port}") from e
    except Exception as e:
        _logger.exception("Failed to connect to roaster")
        raise ConnectionError(f"Failed to connect to roaster: {e}") from e


@mcp.tool()
async def disconnect_roaster(ctx: Any) -> dict[str, Any]:
    """Safely disconnect from the roaster.

    Sends the safety guard clear command before closing the connection.
    Safe to call even if not connected.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict confirming disconnection
    """
    state = get_state_from_context(ctx)

    if state.driver is None or not state.driver.is_connected:
        return {
            "status": "not_connected",
            "message": "Roaster was not connected",
        }

    # Check if roast is active
    if state.is_roast_active:
        return {
            "status": "roast_active",
            "message": "Cannot disconnect while roast is active. Use emergency_stop first if needed.",
            "roast_id": state.roast_context.roast_id,
        }

    await state.disconnect_roaster()
    _logger.info("Disconnected from roaster")

    return {
        "status": "disconnected",
        "message": "Successfully disconnected from roaster",
    }


# =============================================================================
# Event Tools
# =============================================================================


@mcp.tool()
async def execute_charge(
    ctx: Any,
    coffee_id: str | None = None,
    profile_id: str | None = None,
    batch_size_grams: int | None = None,
    confirmed: bool = False,
    drop_id: str | None = None,
) -> dict[str, Any]:
    """Start the roast (CHARGE event).

    REQUIRES HUMAN CONFIRMATION. This marks the moment beans are dropped
    into the preheated drum and begins the roast timer.

    If confirmed=False, returns a confirmation request.
    If confirmed=True, executes the CHARGE.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: Optional ID of the coffee being roasted
        profile_id: Optional ID of the profile to follow
        batch_size_grams: Batch size in grams
        confirmed: Must be True to execute the charge

    Returns:
        Confirmation request if not confirmed, or charge confirmation with roast data
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

    # Get current state for confirmation message
    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)

    if not confirmed:
        return {
            "requires_confirmation": True,
            "action": "execute_charge",
            "message": (
                f"Ready to CHARGE at {current_bt:.1f}°C BT. "
                "This will start the roast timer. "
                "Call execute_charge with confirmed=True to proceed."
            ),
            "current_bt": current_bt,
            "coffee_id": coffee_id,
            "profile_id": profile_id,
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

    # Create RoastSession (handles state machine transitions)
    # Pass existing machine if preheat created one
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

    # Update legacy roast context for backward compatibility
    state.roast_context.is_active = True
    state.roast_context.charge_time = charge_time.timestamp()
    state.roast_context.coffee_id = coffee_id
    state.roast_context.profile_id = profile_id
    state.roast_context.roast_id = roast_id

    if state._heating_manager is not None:
        state._heating_manager.finish_preheat()
    if state._monitor is not None:
        await state._monitor.stop()
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

    _logger.info("CHARGE executed at %.1f°C (roast_id=%s)", current_bt, roast_id)

    return {
        "status": "charged",
        "message": f"CHARGE executed at {current_bt:.1f}°C. Roast timer started.",
        "charge_temp": current_bt,
        "charge_time": charge_time.isoformat(),
        "roast_id": roast_id,
        "coffee_id": coffee_id,
        "profile_id": profile_id,
        "batch_size_grams": batch_size_grams,
    }


@mcp.tool()
async def mark_turning_point(ctx: Any) -> dict[str, Any]:
    """Mark the turning point (lowest BT after charge).

    The turning point is the lowest bean temperature reached after charge,
    typically 1-2 minutes in. After this point, temperature starts rising.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict with turning point data
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    if not state.is_roast_active or state.roast_session is None:
        return {"status": "error", "error": "No active roast. Execute charge first."}

    _reset_safety_watchdog(state)

    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = _get_elapsed_seconds(state)
    time_secs = int(elapsed) if elapsed else 0

    try:
        state.roast_session.mark_event("turning_point", temp=current_bt, time_seconds=float(time_secs))
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e)}

    # Trigger event on roaster hardware
    await state.driver.trigger_event(RoastEvent.TURNING_POINT)

    # Update DB
    await _update_roast_event(state, turning_point_time=time_secs, turning_point_temp=current_bt)

    _logger.info("Turning point marked at %.1f°C after %ds", current_bt, time_secs)

    return {
        "status": "marked",
        "event": "turning_point",
        "temp": current_bt,
        "time_seconds": time_secs,
        "message": f"Turning point marked at {current_bt:.1f}°C ({time_secs}s from charge)",
    }


@mcp.tool()
async def mark_dry_end(ctx: Any) -> dict[str, Any]:
    """Mark the end of the drying phase.

    Drying phase ends when moisture has evaporated, typically around 150°C.
    This marks the transition to the Maillard phase.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict with dry end data
    """
    state = get_state_from_context(ctx)

    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    if not state.is_roast_active or state.roast_session is None:
        return {"status": "error", "error": "No active roast. Execute charge first."}

    _reset_safety_watchdog(state)

    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = _get_elapsed_seconds(state)
    time_secs = int(elapsed) if elapsed else 0

    try:
        state.roast_session.mark_event("dry_end", temp=current_bt, time_seconds=float(time_secs))
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e)}

    await state.driver.trigger_event(RoastEvent.DRY_END)
    await _update_roast_event(state, dry_end_time=time_secs, dry_end_temp=current_bt)

    _logger.info("Dry end marked at %.1f°C after %ds", current_bt, time_secs)

    return {
        "status": "marked",
        "event": "dry_end",
        "temp": current_bt,
        "time_seconds": time_secs,
        "message": f"Dry end marked at {current_bt:.1f}°C ({time_secs}s). Entering Maillard phase.",
    }


@mcp.tool()
async def mark_first_crack(ctx: Any, confirmed: bool = False) -> dict[str, Any]:
    """Mark first crack start.

    REQUIRES HUMAN CONFIRMATION to ensure crack was actually heard.
    First crack typically occurs around 196°C and marks the start
    of the development phase.

    Args:
        ctx: MCP context (injected automatically)
        confirmed: Must be True to mark first crack

    Returns:
        Confirmation request if not confirmed, or first crack data
    """
    state = get_state_from_context(ctx)

    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    if not state.is_roast_active or state.roast_session is None:
        return {"status": "error", "error": "No active roast. Execute charge first."}

    _reset_safety_watchdog(state)

    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = _get_elapsed_seconds(state)
    time_secs = int(elapsed) if elapsed else 0

    if not confirmed:
        return {
            "requires_confirmation": True,
            "action": "mark_first_crack",
            "message": (
                f"Ready to mark FIRST CRACK at {current_bt:.1f}°C ({time_secs}s). "
                "Confirm you heard the crack. "
                "Call mark_first_crack with confirmed=True to proceed."
            ),
            "current_bt": current_bt,
            "elapsed_seconds": time_secs,
        }

    try:
        state.roast_session.mark_event("fc_start", temp=current_bt, time_seconds=float(time_secs))
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e)}

    await state.driver.trigger_event(RoastEvent.FC_START)
    await _update_roast_event(state, fc_start_time=time_secs, fc_start_temp=current_bt)

    _logger.info("First crack marked at %.1f°C after %ds", current_bt, time_secs)

    return {
        "status": "marked",
        "event": "fc_start",
        "temp": current_bt,
        "time_seconds": time_secs,
        "message": f"First crack marked at {current_bt:.1f}°C ({time_secs}s). Entering development phase.",
    }


@mcp.tool()
async def mark_first_crack_end(ctx: Any) -> dict[str, Any]:
    """Mark the end of first crack.

    Call this when first crack sounds have stopped.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict with first crack end data
    """
    state = get_state_from_context(ctx)

    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    if not state.is_roast_active or state.roast_session is None:
        return {"status": "error", "error": "No active roast. Execute charge first."}

    _reset_safety_watchdog(state)

    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = _get_elapsed_seconds(state)
    time_secs = int(elapsed) if elapsed else 0

    try:
        state.roast_session.mark_event("fc_end", temp=current_bt, time_seconds=float(time_secs))
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e)}

    await state.driver.trigger_event(RoastEvent.FC_END)
    await _update_roast_event(state, fc_end_time=time_secs, fc_end_temp=current_bt)

    _logger.info("First crack end marked at %.1f°C after %ds", current_bt, time_secs)

    return {
        "status": "marked",
        "event": "fc_end",
        "temp": current_bt,
        "time_seconds": time_secs,
        "message": f"First crack ended at {current_bt:.1f}°C ({time_secs}s)",
    }


@mcp.tool()
async def mark_second_crack(ctx: Any) -> dict[str, Any]:
    """Mark second crack start (for darker roasts).

    Second crack typically occurs around 224°C. Only relevant for
    medium-dark to dark roasts.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict with second crack data
    """
    state = get_state_from_context(ctx)

    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    if not state.is_roast_active or state.roast_session is None:
        return {"status": "error", "error": "No active roast. Execute charge first."}

    _reset_safety_watchdog(state)

    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = _get_elapsed_seconds(state)
    time_secs = int(elapsed) if elapsed else 0

    try:
        state.roast_session.mark_event("sc_start", temp=current_bt, time_seconds=float(time_secs))
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e)}

    await state.driver.trigger_event(RoastEvent.SC_START)
    await _update_roast_event(state, sc_start_time=time_secs, sc_start_temp=current_bt)

    _logger.info("Second crack marked at %.1f°C after %ds", current_bt, time_secs)

    return {
        "status": "marked",
        "event": "sc_start",
        "temp": current_bt,
        "time_seconds": time_secs,
        "message": f"Second crack marked at {current_bt:.1f}°C ({time_secs}s). Consider dropping soon!",
    }


@mcp.tool()
async def execute_drop(ctx: Any, confirmed: bool = False) -> dict[str, Any]:
    """End the roast (DROP event).

    REQUIRES HUMAN CONFIRMATION. This marks the end of the roast when
    beans are dropped from the drum to the cooling tray.

    Args:
        ctx: MCP context (injected automatically)
        confirmed: Must be True to execute drop

    Returns:
        Confirmation request if not confirmed, or roast summary after drop
    """
    state = get_state_from_context(ctx)

    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    if not state.is_roast_active or state.roast_session is None:
        return {"status": "error", "error": "No active roast to drop."}

    session = state.roast_session
    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = _get_elapsed_seconds(state)
    total_seconds = int(elapsed) if elapsed else 0

    if not confirmed:
        return {
            "requires_confirmation": True,
            "action": "execute_drop",
            "message": (
                f"Ready to DROP at {current_bt:.1f}°C ({total_seconds}s). "
                "This will end the roast. "
                "Call execute_drop with confirmed=True to proceed."
            ),
            "current_bt": current_bt,
            "elapsed_seconds": total_seconds,
        }

    # Stop heating manager first (safety: stop heat before other cleanup)
    await _stop_heating_manager(state)

    # Stop monitoring before finalize (order: roast monitor first, then safety)
    if state._monitor is not None:
        await state._monitor.stop()
        state._monitor = None

    if state._safety_monitor is not None:
        await state._safety_monitor.stop()
        state._safety_monitor = None

    # Trigger DROP on hardware
    await state.driver.trigger_event(RoastEvent.DROP)

    # State machine transitions: drop → cooling → completed
    try:
        session.machine.drop(temp=current_bt, time_seconds=float(total_seconds))
        session.machine.complete()
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e)}

    # Record drop event in collector
    session.collector.add_event("drop", float(total_seconds), current_bt)

    # Finalize curve data
    curve_data = session.finalize(status="completed")
    curve_dict = curve_data.model_dump()

    # Calculate DTR
    dtr = session.collector.calculate_dtr(drop_time=float(total_seconds))

    # Persist to DB
    drop_time = datetime.now(UTC)
    if session.roast_id is not None:
        roast = await Roast.get(session.roast_id)
        if roast is not None:
            for field, value in derive_roast_event_fields(curve_data.model_dump()).items():
                setattr(roast, field, value)
            roast.drop_time = total_seconds
            roast.drop_temp = current_bt
            roast.end_time = drop_time.isoformat()
            roast.total_time_seconds = total_seconds
            roast.connection_events = session.connection_events
            roast.curve_data = curve_dict
            roast.dtr = dtr
            roast.status = RoastStatus.COMPLETED
            await roast.set({field: getattr(roast, field) for field in (
                *derive_roast_event_fields(curve_data.model_dump()), "drop_time", "drop_temp",
                "end_time", "total_time_seconds", "curve_data", "connection_events", "dtr", "status")})
            from maillard.inventory.service import deduct_finished_roast
            await deduct_finished_roast(roast)

    _logger.info("DROP executed at %.1f°C after %ds (roast_id=%s)", current_bt, total_seconds, session.roast_id)

    summary: dict[str, Any] = {
        "status": "dropped",
        "message": f"DROP executed at {current_bt:.1f}°C after {total_seconds}s",
        "roast_id": session.roast_id,
        "drop_temp": current_bt,
        "total_time_seconds": total_seconds,
        "drop_time": drop_time.isoformat(),
        "dtr": dtr,
        "coffee_id": session.coffee_id,
        "profile_id": session.profile_id,
        "sample_count": session.sample_count,
    }

    # Clear session
    state.clear_roast_context()

    return summary


# =============================================================================
# Control Tools
# =============================================================================


@mcp.tool()
async def set_heat(ctx: Any, percent: int) -> dict[str, Any]:
    """Set heating power (0-100%).

    Controls the heating element power. Higher values mean more heat.
    Use carefully - excessive heat can scorch beans.

    Note: If the execution engine is in PROFILE mode, calling this will
    auto-switch to MANUAL mode.

    Note: This starts a background HeatingManager that continuously maintains
    the heating state (HS=1). The Kaleido M10 requires HS to be re-sent
    continuously or the firmware will revert it to 0.

    Args:
        ctx: MCP context (injected automatically)
        percent: Heating power percentage (0-100)

    Returns:
        Dict confirming heat setting
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    # Validate range
    if not 0 <= percent <= 100:
        raise ValueError(f"Heat percent must be 0-100, got {percent}")

    _reset_safety_watchdog(state)
    _notify_manual_override(state, "heat", percent)

    # Start/stop HeatingManager based on power level
    # The HeatingManager continuously sends HS=1 and HP=percent to maintain heat
    await _start_heating_manager(state, percent)

    # Also call driver.set_heat for immediate effect (HeatingManager will maintain)
    await state.driver.set_heat(percent)

    _logger.info("Heat set to %d%%", percent)

    return {
        "status": "set",
        "control": "heat",
        "percent": percent,
        "heating_enabled": percent > 0,
        "heating_manager_active": state._heating_manager is not None and state._heating_manager.is_running,
        "message": f"Heating power set to {percent}%",
    }


@mcp.tool()
async def set_fan(ctx: Any, percent: int) -> dict[str, Any]:
    """Set fan speed (0-100%).

    Controls airflow through the roaster. Higher fan speeds:
    - Increase convection heat transfer
    - Help remove chaff and smoke
    - Can cool the roast if heat is low

    Note: If the execution engine is in PROFILE mode, calling this will
    auto-switch to MANUAL mode.

    Args:
        ctx: MCP context (injected automatically)
        percent: Fan speed percentage (0-100)

    Returns:
        Dict confirming fan setting
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    # Validate range
    if not 0 <= percent <= 100:
        raise ValueError(f"Fan percent must be 0-100, got {percent}")

    _reset_safety_watchdog(state)
    _notify_manual_override(state, "fan", percent)
    await state.driver.set_fan(percent)

    _logger.info("Fan set to %d%%", percent)

    return {
        "status": "set",
        "control": "fan",
        "percent": percent,
        "message": f"Fan speed set to {percent}%",
    }


@mcp.tool()
async def set_drum(ctx: Any, percent: int) -> dict[str, Any]:
    """Set drum rotation speed (0-100%).

    Controls how fast the drum rotates. Proper drum speed ensures
    even roasting and prevents scorching.

    Note: If the execution engine is in PROFILE mode, calling this will
    auto-switch to MANUAL mode.

    Args:
        ctx: MCP context (injected automatically)
        percent: Drum speed percentage (0-100)

    Returns:
        Dict confirming drum setting
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    # Validate range
    if not 0 <= percent <= 100:
        raise ValueError(f"Drum percent must be 0-100, got {percent}")

    _reset_safety_watchdog(state)
    _notify_manual_override(state, "drum", percent)
    await state.driver.set_drum(percent)

    _logger.info("Drum set to %d%%", percent)

    return {
        "status": "set",
        "control": "drum",
        "percent": percent,
        "message": f"Drum speed set to {percent}%",
    }


@mcp.tool()
async def set_target_temp(ctx: Any, temp_celsius: float) -> dict[str, Any]:
    """Set the PID target temperature.

    When PID mode is enabled, the roaster will automatically adjust
    heat to reach and maintain this temperature.

    Args:
        ctx: MCP context (injected automatically)
        temp_celsius: Target temperature in Celsius

    Returns:
        Dict confirming target setting
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    # Basic validation
    if temp_celsius < 0 or temp_celsius > 300:
        raise ValueError(f"Temperature must be 0-300°C, got {temp_celsius}")

    _reset_safety_watchdog(state)
    await state.driver.set_target_temp(temp_celsius)

    _logger.info("Target temp set to %.1f°C", temp_celsius)

    return {
        "status": "set",
        "control": "target_temp",
        "temp_celsius": temp_celsius,
        "message": f"PID target temperature set to {temp_celsius:.1f}°C",
    }


@mcp.tool()
async def toggle_pid(ctx: Any, enabled: bool) -> dict[str, Any]:
    """Enable or disable the roaster's internal PID auto-heating mode.

    NOTE: The Kaleido M10's internal PID may not work reliably via API.
    When enabled via API, the heat power (HP) often drops to 0%.
    Consider using manual heat control (set_heat) instead.

    When enabled, the roaster should automatically adjust heat power to
    reach and maintain the target temperature. When disabled, you
    have manual control over heat power.

    Args:
        ctx: MCP context (injected automatically)
        enabled: True to enable PID, False for manual control

    Returns:
        Dict confirming PID mode
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    _reset_safety_watchdog(state)
    await state.driver.set_pid_mode(enabled)

    mode = "enabled" if enabled else "disabled"
    _logger.info("PID mode %s", mode)

    return {
        "status": "set",
        "control": "pid",
        "enabled": enabled,
        "message": f"PID auto-heating mode {mode}",
    }


@mcp.tool()
async def emergency_stop(ctx: Any) -> dict[str, Any]:
    """IMMEDIATE SAFETY SHUTDOWN.

    Sets heat to 0% and fan to 100% for rapid cooling.
    NO CONFIRMATION REQUIRED - use this immediately if there's any safety concern.

    This will abort any active roast.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        Dict confirming emergency stop
    """
    state = get_state_from_context(ctx)

    # Halt local producers even if hardware is unreachable.
    await state.halt_heat_sources()
    if state.driver is None:
        raise ConnectionError("Local heat sources halted; use Kaleido controls")

    # Execute emergency stop on hardware
    confirmed = False
    try:
        confirmed = await state.driver.emergency_stop() is True
    except Exception:
        _logger.critical("MCP emergency stop failed", exc_info=True)

    # Disable heating state for extra safety, independently of HP command failure.
    try:
        await state.driver.set_heating_state(False)
    except Exception:
        _logger.critical("MCP heat-off failed", exc_info=True)
    if not confirmed:
        from maillard.core.events import SafetyAlertEvent, SafetyAlertLevel

        await state.event_bus.publish(SafetyAlertEvent(
            level=SafetyAlertLevel.CRITICAL,
            message="EMERGENCY STOP COMMAND FAILED - CUT HEAT ON ROASTER",
            condition="emergency_stop_failed",
        ))

    _logger.warning("EMERGENCY STOP confirmed=%s", confirmed)

    # Stop monitoring (order: roast monitor first, then safety)
    if state._monitor is not None:
        await state._monitor.stop()
        state._monitor = None

    if state._safety_monitor is not None:
        await state._safety_monitor.stop()
        state._safety_monitor = None

    # Handle active session
    was_active = state.is_roast_active
    roast_id = state.roast_context.roast_id
    session = state.roast_session

    if was_active and session is not None:
        # Abort state machine
        with suppress(InvalidStateError, TransitionNotAllowed):
            session.machine.abort(reason="Emergency stop")

        # Finalize with aborted status
        curve_data = session.finalize(status="aborted")
        curve_dict = curve_data.model_dump()

        # Persist partial data to DB
        if session.roast_id is not None:
            try:
                roast = await Roast.get(session.roast_id)
                if roast is not None:
                    roast.status = RoastStatus.ABORTED
                    roast.end_time = datetime.now(UTC).isoformat()
                    elapsed = session.elapsed_time
                    if elapsed is not None:
                        roast.total_time_seconds = int(elapsed)
                    roast.connection_events = session.connection_events
                    roast.curve_data = curve_dict
                    await roast.set({"status": roast.status, "end_time": roast.end_time,
                                     "total_time_seconds": roast.total_time_seconds,
                                     "connection_events": roast.connection_events, "curve_data": roast.curve_data})
                    from maillard.inventory.service import deduct_finished_roast
                    await deduct_finished_roast(roast)
            except Exception:
                _logger.warning("Failed to persist abort data", exc_info=True)

    state.clear_roast_context()

    return {
        "status": "emergency_stopped" if confirmed else "emergency_stop_unconfirmed",
        "message": ("EMERGENCY STOP executed. Heat=0%, Fan=100%." if confirmed
                    else "EMERGENCY STOP UNCONFIRMED - CUT HEAT ON ROASTER"),
        "confirmed": confirmed,
        "heat_percent": 0 if confirmed else None,
        "fan_percent": 100 if confirmed else None,
        "roast_aborted": was_active,
        "aborted_roast_id": roast_id if was_active else None,
    }


# =============================================================================
# Preheat Tool
# =============================================================================


@mcp.tool()
async def start_preheat(
    ctx: Any,
    target_temp: float,
    heat_percent: int = 100,
    fan_percent: int = 30,
    drum_percent: int = 50,
) -> dict[str, Any]:
    """Begin preheating to the specified charge temperature.

    Sets up the roaster for preheating with manual heat control. The roaster's
    internal PID doesn't work reliably via API, so we use manual heat control.

    Default settings are 100% heat, 30% fan, 50% drum for fast preheat.
    Reduce heat as you approach target to avoid overshoot.

    Args:
        ctx: MCP context (injected automatically)
        target_temp: Target charge temperature in Celsius (typically 180-220°C)
        heat_percent: Initial heating power (0-100, default 100)
        fan_percent: Fan speed (0-100, default 30)
        drum_percent: Drum speed (0-100, default 50)

    Returns:
        Dict with preheat status
    """
    state = get_state_from_context(ctx)

    # Validate connection
    if state.driver is None or not state.driver.is_connected:
        raise ConnectionError("Roaster not connected")

    # Validate temperature
    if target_temp < 100 or target_temp > 250:
        raise ValueError(f"Preheat temperature should be 100-250°C, got {target_temp}")

    # Check if roast is active
    if state.is_roast_active:
        return {"status": "error", "error": "Cannot preheat while roast is active"}

    # Set controls: drum and fan must be running for physical response
    await state.driver.set_drum(drum_percent)
    await state.driver.set_fan(fan_percent)

    # Start HeatingManager - continuously maintains HS=1 and HP
    # This is required because the Kaleido firmware reverts HS to 0 otherwise
    await _start_heating_manager(state, heat_percent)

    if state._heating_manager is not None:
        await state._heating_manager.start(
            heat_percent, preheat_timeout=state.config.preheat_timeout_seconds
        )
    # Also call driver.set_heat for immediate effect
    await state.driver.set_heat(heat_percent)

    # Store target for reference (used by monitoring tools)
    await state.driver.set_target_temp(target_temp)

    # Create state machine in PREHEATING state if no session exists
    if state.roast_session is None:
        from maillard.roasting.state import RoastStateMachine

        machine = RoastStateMachine()
        machine.start_preheat(target_temp=target_temp)
        session = RoastSession(
            machine=machine,
            collector=CurveCollector(),
            event_bus=state.event_bus,
        )
        state.roast_session = session

    if state._monitor is not None:
        await state._monitor.stop()
    state._monitor = RoastMonitor(
        state.driver, state.event_bus, state.roast_session,
        poll_interval=state.config.poll_interval,
        stale_alarm_seconds=state.config.stale_alarm_seconds,
        alarm_clear_seconds=state.config.alarm_clear_seconds,
    )
    await state._monitor.start()
    # Get current temperature
    driver_state = await state.driver.read_state()
    current_bt = driver_state.get("BT", 0)

    _logger.info(
        "Preheating to %.1f°C (current: %.1f°C) - H:%d%% F:%d%% D:%d%%",
        target_temp, current_bt, heat_percent, fan_percent, drum_percent
    )

    return {
        "status": "preheating",
        "target_temp": target_temp,
        "current_bt": current_bt,
        "heat_percent": heat_percent,
        "fan_percent": fan_percent,
        "drum_percent": drum_percent,
        "heating_enabled": True,
        "message": (
            f"Preheating to {target_temp:.1f}°C. Currently at {current_bt:.1f}°C. "
            f"Heat={heat_percent}%, Fan={fan_percent}%, Drum={drum_percent}%. "
            "Reduce heat as you approach target to avoid overshoot."
        ),
    }


__all__ = [
    "connect_roaster",
    "disconnect_roaster",
    "emergency_stop",
    "execute_charge",
    "execute_drop",
    "get_roaster_status",
    "mark_dry_end",
    "mark_first_crack",
    "mark_first_crack_end",
    "mark_second_crack",
    "mark_turning_point",
    "set_drum",
    "set_fan",
    "set_heat",
    "set_target_temp",
    "start_preheat",
    "toggle_pid",
]
