"""REST API routes for the Maillard web dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from maillard.db.converters import (
    cupping_to_response,
    profile_to_response,
    roast_to_response,
    roast_to_response_with_curve,
)
from maillard.db.models import PresaleDrop, Profile, Roast, RoastStatus
from maillard.db.repository import (
    CoffeeRepository,
    CuppingRepository,
    ProfileRepository,
    RoastRepository,
)
from maillard.inventory.service import coffee_api_response, deduct_for_roast
from maillard.mcp.state import ApplicationState
from maillard.roasting.events import derive_roast_event_fields

_logger = logging.getLogger(__name__)


_sync_tasks: set[asyncio.Task] = set()


def _site_host(site_url: str) -> str:
    host = urlsplit((site_url or "").strip()).netloc.removeprefix("www.")
    return host or "roastery.example.com"


def _enqueue_site_sync(roast_id: str) -> None:
    async def enqueue() -> None:
        try:
            from maillard.sync.site import enqueue_roast_sync

            await enqueue_roast_sync(roast_id)
        except Exception:
            _logger.exception("Could not enqueue roast %s", roast_id)

    task = asyncio.create_task(enqueue())
    _sync_tasks.add(task)
    task.add_done_callback(_sync_tasks.discard)


def _get_state(request: Request) -> ApplicationState:
    return request.app.state.maillard  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CoolingRequest(BaseModel):
    enabled: bool = True


class RoastNotesRequest(BaseModel):
    notes: str | None = None


class AIContextRequest(BaseModel):
    bean_name: str | None = None
    roast_level: str | None = None
    batch_grams: int | None = None


class RecommendRequest(BaseModel):
    coffee_id: str
    profile_id: str | None = None
    batch_size_grams: int | None = None


class GenerateProfileRequest(BaseModel):
    coffee_id: str
    roast_level: str = "Medium"
    flavor_goal: str = ""
    brew_method: str = "Filter"
    batch_size_grams: int = 1200


class ControlsRequest(BaseModel):
    heat: int | None = Field(default=None, ge=0, le=100)
    fan: int | None = Field(default=None, ge=0, le=100)
    drum: int | None = Field(default=None, ge=0, le=100)


class AIToggleRequest(BaseModel):
    mode: str  # "off", "suggest", "auto"


class AutoDropRequest(BaseModel):
    enabled: bool = False


class AIConfirmRequest(BaseModel):
    heat: int | None = None
    fan: int | None = None
    drum: int | None = None
    event: str | None = None
    drop: bool = False


class AIMessageRequest(BaseModel):
    message: str


class AIActionRequest(BaseModel):
    action: str  # accepted, rejected, dismissed
    suggestion: dict[str, Any] | None = None


class ConnectRequest(BaseModel):
    driver_type: str = "kaleido"
    host: str | None = None
    port: int | None = None


class PreheatRequest(BaseModel):
    target_temp: float = Field(ge=100, le=260)
    heat_percent: int = Field(default=100, ge=0, le=100)
    fan_percent: int = Field(default=30, ge=0, le=100)
    drum_percent: int = Field(default=50, ge=0, le=100)


class RoastWeightsRequest(BaseModel):
    green_weight_grams: float | None = Field(default=None, ge=0, le=5000, allow_inf_nan=False)
    roasted_weight_grams: float | None = Field(default=None, ge=0, le=5000, allow_inf_nan=False)
    bags_packed: int | None = Field(default=None, ge=0)
    bag_size_grams: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    drop_id: str | None = None


class ChargeRequest(BaseModel):
    drop_id: str | None = None
    coffee_id: str | None = None
    profile_id: str | None = None
    batch_size_grams: int | None = None


# ---------------------------------------------------------------------------
# Helpers (reuse MCP tool internals where possible)
# ---------------------------------------------------------------------------


async def _roaster_state_summary(state: ApplicationState) -> dict[str, Any]:
    """Build a roaster state summary dict."""
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


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


async def drop_active_roast(
    state: ApplicationState,
    *,
    agent: Any = None,
    use_detected: bool = True,
) -> dict[str, Any]:
    """End the roast: drop event first, cooling tray (CS) last."""
    if state.driver is None or not state.driver.is_connected:
        return {"status": "error", "error": "Roaster not connected"}
    if state.roast_session is None or not state.roast_session.is_active:
        return {"status": "error", "error": "No active roast"}

    from statemachine.exceptions import TransitionNotAllowed

    from maillard.driver.base import RoastEvent
    from maillard.roasting.state import InvalidStateError

    session = state.roast_session
    driver_state = state.driver.state
    current_bt = driver_state.get("BT", 0)
    elapsed = session.elapsed_time or 0
    total_seconds = int(elapsed)
    if use_detected and session.detected_drop:
        total_seconds = int(session.detected_drop["time_seconds"])
        current_bt = session.detected_drop["temp"]

    if agent is not None:
        block = getattr(agent, "block_auto_resume", None)
        if callable(block):
            block()

    if state._heating_manager is not None:
        await state._heating_manager.stop()
        state._heating_manager = None
    await state.driver.set_heat(0)
    await state.driver.set_fan(100)
    await state.driver.trigger_event(RoastEvent.DROP)
    cooling = await state.cooling_fan().set_enabled(
        state.driver, True, auto=True, schedule_off=True
    )

    if state._monitor is not None:
        await state._monitor.stop()
        state._monitor = None
    if state._safety_monitor is not None:
        await state._safety_monitor.stop()
        state._safety_monitor = None
    if state._execution_engine is not None:
        await state._execution_engine.stop()
        state._execution_engine = None

    try:
        session.machine.drop(temp=current_bt, time_seconds=float(total_seconds))
        session.machine.complete()
    except (InvalidStateError, TransitionNotAllowed) as e:
        return {"status": "error", "error": str(e), "cooling": cooling}

    session.collector.add_event("drop", float(total_seconds), current_bt)
    curve_data = session.finalize(status="completed")
    dtr = session.collector.calculate_dtr(drop_time=float(total_seconds))

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
            roast.curve_data = curve_data.model_dump()
            roast.dtr = dtr
            roast.status = RoastStatus.COMPLETED
            await roast.set(
                {
                    field: getattr(roast, field)
                    for field in (
                        *derive_roast_event_fields(curve_data.model_dump()),
                        "drop_time",
                        "drop_temp",
                        "end_time",
                        "total_time_seconds",
                        "curve_data",
                        "connection_events",
                        "dtr",
                        "status",
                    )
                }
            )
            from maillard.inventory.service import deduct_finished_roast

            await deduct_finished_roast(roast)

    if agent is not None:
        comparison_metrics = {
            "drop_temp": current_bt,
            "total_time_seconds": total_seconds,
            "dtr": dtr,
            "fc_start_time": session.machine.fc_start_time,
            "dry_end_time": session.machine.dry_end_time,
            "turning_point_time": session.machine.turning_point_time,
        }
        asyncio.create_task(agent.generate_post_roast_comparison(comparison_metrics))

    if session.roast_id:
        _enqueue_site_sync(session.roast_id)

    roast_id = session.roast_id
    state.clear_roast_context()
    return {
        "status": "dropped",
        "roast_id": roast_id,
        "drop_temp": current_bt,
        "total_time_seconds": total_seconds,
        "dtr": dtr,
        "cooling": cooling,
    }


def create_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    # -- Status -------------------------------------------------------------

    @router.get("/status")
    async def get_status(request: Request) -> dict[str, Any]:
        state = _get_state(request)
        roaster = await _roaster_state_summary(state)

        session = state.roast_session
        roast_info: dict[str, Any] = {
            "active": state.is_roast_active,
            "roast_id": state.roast_context.roast_id,
        }
        if session is not None and session.is_active:
            elapsed = session.elapsed_time
            roast_info["elapsed_seconds"] = round(elapsed, 1) if elapsed else None
            roast_info["state"] = session.state_enum.value
            phase = session.current_phase
            roast_info["phase"] = phase.name if phase else None
            roast_info["latest_ror"] = round(session.latest_ror, 1) if session.latest_ror else None
            roast_info["sample_count"] = session.sample_count
            roast_info["detected_drop"] = session.detected_drop
            roast_info["auto_events"] = sorted(session.auto_events)

        # Execution engine status
        engine_info: dict[str, Any] | None = None
        if state._execution_engine is not None:
            engine_info = state._execution_engine.get_status()

        agent = getattr(request.app.state, "ai_agent", None)
        ai_info = None
        if agent is not None:
            ai_info = {
                "mode": agent.status_mode,
                "auto_control_enabled": bool(getattr(agent, "auto_control_enabled", False)),
            }

        return {
            "roaster": roaster,
            "roast": roast_info,
            "execution": engine_info,
            "heating": state._heating_manager.get_status() if state._heating_manager else None,
            "cooling": state.cooling_fan().get_status(),
            "auto_drop": bool(getattr(state.config, "auto_drop", False)),
            "ai": ai_info,
            "monitoring": (
                {
                    "connection_lost": state._monitor.connection_lost,
                    "link_state": state._monitor.link_state,
                    "data_age_s": state._monitor.data_age_s,
                }
                if state._monitor
                else None
            ),
            "driver_type": getattr(state, "_driver_type", "simulator"),
            "printer_configured": bool(state.config.zebra_host or state.config.zebra_cups_name),
        }

    # -- Connect / Disconnect ------------------------------------------------

    @router.post("/connect")
    async def connect_roaster(request: Request, body: ConnectRequest) -> dict[str, Any]:
        state = _get_state(request)

        if state.driver is not None and state.driver.is_connected:
            return {"status": "already_connected"}

        dtype = body.driver_type
        if dtype not in ("kaleido", "simulator"):
            return {"status": "error", "error": f"Invalid driver_type: {dtype}"}

        try:
            await state.connect_roaster(
                driver_type=dtype,  # type: ignore[arg-type]
                host=body.host,
                port=body.port,
            )
        except Exception as e:
            return {"status": "error", "error": str(e)}

        roaster = await _roaster_state_summary(state)
        return {"status": "connected", "roaster": roaster}

    @router.post("/disconnect")
    async def disconnect_roaster(request: Request) -> dict[str, Any]:
        state = _get_state(request)

        if state.driver is None:
            return {"status": "not_connected"}

        # Stop heating manager if running
        if state._heating_manager is not None:
            await state._heating_manager.stop()
            state._heating_manager = None

        # Stop monitors if running
        if state._monitor is not None:
            await state._monitor.stop()
            state._monitor = None
        if state._safety_monitor is not None:
            await state._safety_monitor.stop()
            state._safety_monitor = None

        # Clear roast state
        if state.is_roast_active and state.roast_session is not None:
            with contextlib.suppress(Exception):
                state.roast_session.finalize(status="aborted")
        state.clear_roast_context()

        await state.disconnect_roaster()
        return {"status": "disconnected"}

    # -- Roast progress -----------------------------------------------------

    @router.get("/roast")
    async def get_roast(request: Request) -> dict[str, Any]:
        state = _get_state(request)

        if state.roast_session is None or not state.roast_session.is_active:
            return {"status": "no_active_roast"}

        session = state.roast_session
        bt: float | None = None
        et: float | None = None
        if state.driver is not None and state.driver.is_connected:
            ds = state.driver.state
            bt = ds.get("BT")
            et = ds.get("ET")

        ror_instant = session.latest_ror
        ror_smoothed: float | None = None
        ror_list = session.collector.calculate_ror(window_seconds=30)
        if ror_list:
            ror_smoothed = ror_list[-1]

        events = [
            {"type": e.type, "time_seconds": e.time_seconds, "temp": e.temp}
            for e in session.collector.events
        ]

        return {
            "status": "active",
            "bt": bt,
            "et": et,
            "ror_instant": round(ror_instant, 1) if ror_instant else None,
            "ror_smoothed": round(ror_smoothed, 1) if ror_smoothed else None,
            "elapsed_seconds": round(session.elapsed_time or 0, 1),
            "current_phase": (session.current_phase.name if session.current_phase else None),
            "sample_count": session.sample_count,
            "events": events,
        }

    # -- Emergency stop -----------------------------------------------------

    @router.post("/emergency-stop")
    async def emergency_stop(request: Request) -> dict[str, Any]:
        state = _get_state(request)

        # Stop heating manager + execution engine first (safety)
        await state.halt_heat_sources()
        agent = _get_ai_agent(request)
        if agent is not None:
            from maillard.ai.agent import AIMode

            block = getattr(agent, "block_auto_resume", None)
            if callable(block):
                block()
            with contextlib.suppress(Exception):
                await agent.set_mode(AIMode.OFF)

        confirmed = False
        if state.driver is not None:
            try:
                confirmed = await state.driver.emergency_stop() is True
            except Exception:
                _logger.critical("Dashboard emergency stop failed", exc_info=True)
            try:
                await state.driver.set_heating_state(False)
            except Exception:
                _logger.critical("Dashboard heat-off failed", exc_info=True)
        message = (
            "Local heat sources halted; roaster HP=0 confirmed."
            if confirmed
            else "Local heat sources halted; roaster unreachable or heat-off unconfirmed - CUT HEAT ON ROASTER using Kaleido controls."
        )
        from maillard.core.events import SafetyAlertEvent, SafetyAlertLevel

        await state.event_bus.publish(
            SafetyAlertEvent(
                level=SafetyAlertLevel.CRITICAL,
                message=message,
                condition="emergency_stop_failed",
                cleared=confirmed,
            )
        )

        _logger.warning("EMERGENCY STOP via web dashboard")

        # Stop monitors
        if state._monitor is not None:
            await state._monitor.stop()
            state._monitor = None
        if state._safety_monitor is not None:
            await state._safety_monitor.stop()
            state._safety_monitor = None

        was_active = state.is_roast_active
        if was_active and state.roast_session is not None:
            with contextlib.suppress(Exception):
                state.roast_session.machine.abort(reason="Emergency stop (web)")
            curve = state.roast_session.finalize(status="aborted")
            if state.roast_session.roast_id:
                roast = await Roast.get(state.roast_session.roast_id)
                if roast:
                    await roast.set(
                        {
                            "status": "aborted",
                            "curve_data": curve.model_dump(),
                            "connection_events": state.roast_session.connection_events,
                            "end_time": datetime.now(UTC).isoformat(),
                        }
                    )
                    from maillard.inventory.service import deduct_finished_roast

                    await deduct_finished_roast(roast)
                    _enqueue_site_sync(roast.id)

        state.clear_roast_context()

        return {
            "status": "emergency_stopped" if confirmed else "emergency_stop_unconfirmed",
            "confirmed": confirmed,
            "message": message,
            "roast_aborted": was_active,
        }

    # -- Reset for new roast ------------------------------------------------

    @router.post("/reset-roast")
    async def reset_roast(request: Request) -> dict[str, Any]:
        state = _get_state(request)

        # Stop heating manager if running
        if state._heating_manager is not None:
            await state._heating_manager.stop()
            state._heating_manager = None

        # Stop monitors if running
        if state._monitor is not None:
            await state._monitor.stop()
            state._monitor = None
        if state._safety_monitor is not None:
            await state._safety_monitor.stop()
            state._safety_monitor = None

        # Finalize any lingering session
        if state.roast_session is not None and state.roast_session.is_active:
            with contextlib.suppress(Exception):
                state.roast_session.finalize(status="aborted")

        state.clear_roast_context()
        return {"status": "reset"}

    # -- Controls -----------------------------------------------------------

    @router.post("/controls")
    async def set_controls(request: Request, body: ControlsRequest) -> dict[str, Any]:
        state = _get_state(request)

        if state.driver is None or not state.driver.is_connected:
            return {"status": "error", "error": "Roaster not connected"}

        result: dict[str, Any] = {"status": "ok"}

        try:
            if body.heat is not None:
                from maillard.core.heating import HeatingManager

                if body.heat > 0:
                    if state._heating_manager is None:
                        state._heating_manager = HeatingManager(
                            state.driver, event_bus=state.event_bus
                        )
                    await state._heating_manager.start(body.heat)
                else:
                    if state._heating_manager is not None:
                        await state._heating_manager.stop()
                        state._heating_manager = None
                await state.driver.set_heat(body.heat)
                result["heat"] = body.heat

            if body.fan is not None:
                await state.driver.set_fan(body.fan)
                result["fan"] = body.fan

            if body.drum is not None:
                await state.driver.set_drum(body.drum)
                result["drum"] = body.drum
        except Exception as e:
            _logger.exception("Error setting controls")
            return {"status": "error", "error": str(e)}

        agent = getattr(request.app.state, "ai_agent", None)
        if agent is not None and (
            body.heat is not None or body.fan is not None or body.drum is not None
        ):
            overridden = await agent.notify_operator_override(
                heat=body.heat, fan=body.fan, drum=body.drum
            )
            if overridden:
                result["ai_mode"] = agent.status_mode
                if state._safety_monitor is not None:
                    from maillard.ai.agent import AIMode

                    state._safety_monitor.set_watchdog_enforced(agent.mode == AIMode.AUTO)

        return result

    # -- Preheat ------------------------------------------------------------

    @router.post("/preheat")
    async def start_preheat(request: Request, body: PreheatRequest) -> dict[str, Any]:
        state = _get_state(request)

        if state.driver is None or not state.driver.is_connected:
            return {"status": "error", "error": "Roaster not connected"}

        if state.is_roast_active:
            return {"status": "error", "error": "Cannot preheat while roast is active"}

        await state.driver.set_drum(body.drum_percent)
        await state.driver.set_fan(body.fan_percent)

        from maillard.core.heating import HeatingManager

        if state._heating_manager is None:
            state._heating_manager = HeatingManager(state.driver, event_bus=state.event_bus)
        await state._heating_manager.start(
            body.heat_percent, preheat_timeout=state.config.preheat_timeout_seconds
        )
        await state.driver.set_heat(body.heat_percent)
        await state.driver.set_target_temp(body.target_temp)

        if state.roast_session is None:
            from maillard.roasting.curve import CurveCollector
            from maillard.roasting.state import RoastStateMachine

            machine = RoastStateMachine()
            machine.start_preheat(target_temp=body.target_temp)
            from maillard.roasting.session import RoastSession

            session = RoastSession(
                machine=machine,
                collector=CurveCollector(),
                event_bus=state.event_bus,
            )
            state.roast_session = session

        from maillard.mcp.monitoring import RoastMonitor

        if state._monitor is not None:
            await state._monitor.stop()
        state._monitor = RoastMonitor(
            state.driver,
            state.event_bus,
            state.roast_session,
            poll_interval=state.config.poll_interval,
            stale_alarm_seconds=state.config.stale_alarm_seconds,
            alarm_clear_seconds=state.config.alarm_clear_seconds,
        )
        await state._monitor.start()
        driver_state = await state.driver.read_state()
        return {
            "status": "preheating",
            "target_temp": body.target_temp,
            "current_bt": driver_state.get("BT", 0),
        }

    # -- AI Agent helper (defined early, used by charge/drop/AI routes) ------

    def _get_ai_agent(request: Request):
        agent = getattr(request.app.state, "ai_agent", None)
        return agent

    def _ai_is_auto(agent: Any) -> bool:
        if agent is None:
            return False
        from maillard.ai.agent import AIMode

        return bool(agent.mode == AIMode.AUTO)

    def _control_mode(agent: Any) -> str:
        """Map AI mode to the roast control mode recorded on the roast."""
        if agent is None:
            return "manual"
        from maillard.ai.agent import AIMode

        return {AIMode.AUTO: "auto", AIMode.SUGGEST: "assist"}.get(agent.mode, "manual")

    def _make_emergency_hook(state: Any, agent: Any):
        """Build the SafetyMonitor hook that stops every heat source."""

        async def _hook() -> None:
            await state.halt_heat_sources()
            if agent is not None and _ai_is_auto(agent):
                from maillard.ai.agent import AIMode

                block = getattr(agent, "block_auto_resume", None)
                if callable(block):
                    block()
                with contextlib.suppress(Exception):
                    await agent.set_mode(AIMode.OFF)

        return _hook

    async def _start_suggestion_engine(
        state: Any,
        profile: Any,
        *,
        level: str | None,
        batch_size_grams: int | None,
    ) -> None:
        """Publish FC and drop suggestions on the web charge path."""
        from maillard.ai.calibration import get_calibration
        from maillard.db.schemas import PhaseSchema
        from maillard.roasting.development import drop_target_c, predicted_fc_c
        from maillard.roasting.execution import ControlMode, RoastExecutionEngine

        if state.roast_session is None or state.driver is None:
            return
        if state._execution_engine is not None:
            with contextlib.suppress(Exception):
                await state._execution_engine.stop()
            state._execution_engine = None
        phases: list[Any] = []
        if profile is not None and getattr(profile, "phases", None):
            try:
                phases = [PhaseSchema.model_validate(phase) for phase in profile.phases]
            except Exception:
                _logger.warning("Profile phases unusable for suggestions", exc_info=True)
                phases = []
        roast_level = level or getattr(profile, "roast_level", None)
        cal = get_calibration(batch_size_grams or 0)
        try:
            engine = RoastExecutionEngine(
                driver=state.driver,
                event_bus=state.event_bus,
                session=state.roast_session,
                profile_phases=phases,
                initial_mode=ControlMode.MANUAL,
                dry_end_threshold=float(cal["dry_end_temp"]),
                fc_threshold=predicted_fc_c(cal, batch_size_grams),
                drop_target_temp=drop_target_c(roast_level, cal, batch_size_grams),
            )
            await engine.start()
            state._execution_engine = engine
        except Exception:
            _logger.warning("FC/drop suggestions unavailable", exc_info=True)

    # -- Charge -------------------------------------------------------------

    @router.post("/charge")
    async def execute_charge(request: Request, body: ChargeRequest) -> dict[str, Any]:
        state = _get_state(request)

        if state.driver is None or not state.driver.is_connected:
            return {"status": "error", "error": "Roaster not connected"}

        # Allow charge from PREHEATING/READY_TO_CHARGE; reject only if already roasting
        if state.roast_session is not None and state.roast_session.machine.is_roasting:
            return {"status": "error", "error": "Roast already active"}

        from maillard.core.safety import SafetyMonitor
        from maillard.driver.base import RoastEvent
        from maillard.mcp.monitoring import RoastMonitor
        from maillard.roasting.session import RoastSession

        driver_state = state.driver.state
        current_bt = driver_state.get("BT", 0)

        if body.drop_id:
            drop = await PresaleDrop.get(body.drop_id)
            if drop is None or drop.coffee_id != body.coffee_id or drop.status == "cancelled":
                raise HTTPException(409, "Select a valid drop for the charged coffee")
        await state.driver.trigger_event(RoastEvent.CHARGE)

        charge_time = datetime.now(UTC)
        roast_record = Roast(
            profile_id=body.profile_id,
            coffee_id=body.coffee_id,
            batch_size_grams=body.batch_size_grams,
            green_weight_grams=float(body.batch_size_grams)
            if body.batch_size_grams is not None
            else None,
            drop_id=body.drop_id,
            start_time=charge_time.isoformat(),
            charge_temp=current_bt,
            status=RoastStatus.IN_PROGRESS,
        )
        await roast_record.insert()
        roast_id = roast_record.id

        # Auto-generate a memorable roast name
        from maillard.utils.naming import generate_roast_name

        roast_record.roast_name = generate_roast_name()
        from maillard.roasting.codes import ensure_roast_code

        await ensure_roast_code(roast_record, save=False)
        if body.batch_size_grams:
            roast_record.green_weight_grams = float(body.batch_size_grams)
        await roast_record.set({"roast_name": roast_record.roast_name, "code": roast_record.code})

        existing_machine = None
        if state.roast_session is not None:
            existing_machine = state.roast_session.machine

        session = RoastSession.start(
            event_bus=state.event_bus,
            charge_temp=current_bt,
            roast_id=roast_id,
            coffee_id=body.coffee_id,
            profile_id=body.profile_id,
            batch_size_grams=body.batch_size_grams,
            machine=existing_machine,
        )
        state.roast_session = session
        state.roast_context.is_active = True
        state.roast_context.charge_time = charge_time.timestamp()
        state.roast_context.coffee_id = body.coffee_id
        state.roast_context.profile_id = body.profile_id
        state.roast_context.roast_id = roast_id

        if state._heating_manager is not None:
            state._heating_manager.finish_preheat()
        if state._monitor is not None:
            await state._monitor.stop()
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

        # Manual-first: the watchdog only e-stops while the AI is in AUTO
        # control. In manual/suggest the operator is in control, so a quiet
        # AI just produces a warning.
        agent = _get_ai_agent(request)
        safety_monitor = SafetyMonitor(
            driver=state.driver,
            event_bus=state.event_bus,
            session=session,
            config=state.config,
            watchdog_enforced=_ai_is_auto(agent),
            on_emergency=_make_emergency_hook(state, agent),
        )
        await safety_monitor.start()
        state._safety_monitor = safety_monitor

        roast_record.control_mode = _control_mode(agent)

        # Freeze the selected profile into a per-roast plan (planned curve).
        plan_payload: dict[str, Any] | None = None
        profile = None
        if body.profile_id:
            from maillard.roasting.plan import build_plan_from_profile, plan_to_dict

            profile = await Profile.get(body.profile_id)
            if profile is not None:
                plan = build_plan_from_profile(
                    profile,
                    batch_size_grams=body.batch_size_grams,
                    coffee_id=body.coffee_id,
                    roast_id=roast_id,
                    max_bt_temp=state.config.max_bt_temp,
                )
                await plan.insert()
                roast_record.plan_id = plan.id
                plan_payload = plan_to_dict(plan)
        await roast_record.set(
            {"control_mode": roast_record.control_mode, "plan_id": roast_record.plan_id}
        )

        # Tell AI agent about the coffee for history lookup
        if agent and body.coffee_id:
            agent.set_pending_coffee_id(body.coffee_id)
        if agent is not None:
            agent.note_charge(roast_id)
        await _start_suggestion_engine(
            state,
            profile,
            level=profile.roast_level if profile is not None else None,
            batch_size_grams=body.batch_size_grams,
        )

        return {
            "status": "charged",
            "roast_id": roast_id,
            "code": roast_record.code,
            "charge_temp": current_bt,
            "plan": plan_payload,
        }

    # -- Plans (planned curve per roast) ------------------------------------

    async def _plan_for_roast(roast_id: str | None) -> dict[str, Any] | None:
        if not roast_id:
            return None
        from maillard.db.models import RoastPlan
        from maillard.roasting.plan import plan_to_dict

        roast = await Roast.get(roast_id)
        if roast is None or not roast.plan_id:
            return None
        plan = await RoastPlan.get(roast.plan_id)
        return plan_to_dict(plan) if plan is not None else None

    @router.get("/roast/plan")
    async def active_roast_plan(request: Request) -> dict[str, Any]:
        """Plan of the roast in progress (used to restore the chart overlay)."""
        state = _get_state(request)
        plan = await _plan_for_roast(state.roast_context.roast_id)
        return {"status": "ok", "plan": plan}

    @router.get("/roasts/{roast_id}/plan")
    async def roast_plan(roast_id: str) -> dict[str, Any]:
        plan = await _plan_for_roast(roast_id)
        if plan is None:
            return {"status": "error", "error": "No plan for this roast"}
        return {"status": "ok", "plan": plan}

    @router.get("/profiles/{profile_id}/planned-curve")
    async def profile_planned_curve(request: Request, profile_id: str) -> dict[str, Any]:
        """Preview a profile's planned curve and warnings before charging."""
        from maillard.roasting.plan import build_plan_from_profile, plan_to_dict

        profile = await Profile.get(profile_id)
        if profile is None:
            return {"status": "error", "error": "Profile not found"}
        batch = request.query_params.get("batch_size_grams")
        plan = build_plan_from_profile(
            profile,
            batch_size_grams=int(batch) if batch and batch.isdigit() else None,
            max_bt_temp=_get_state(request).config.max_bt_temp,
            lock=False,
        )
        return {"status": "ok", "plan": plan_to_dict(plan)}

    # -- Events (turning_point, dry_end, first_crack, drop) -----------------

    @router.post("/event/{name}")
    async def mark_event(request: Request, name: str) -> dict[str, Any]:
        state = _get_state(request)

        if state.driver is None or not state.driver.is_connected:
            return {"status": "error", "error": "Roaster not connected"}

        if state.roast_session is None or not state.roast_session.is_active:
            return {"status": "error", "error": "No active roast"}

        from statemachine.exceptions import TransitionNotAllowed

        from maillard.driver.base import RoastEvent
        from maillard.roasting.curve import DuplicateEventError
        from maillard.roasting.state import InvalidStateError

        event_map = {
            "turning_point": RoastEvent.TURNING_POINT,
            "dry_end": RoastEvent.DRY_END,
            "first_crack": RoastEvent.FC_START,
            "fc_end": RoastEvent.FC_END,
            "second_crack": RoastEvent.SC_START,
        }

        if name not in event_map:
            return {"status": "error", "error": f"Unknown event: {name}"}

        driver_state = state.driver.state
        current_bt = driver_state.get("BT", 0)
        elapsed = state.roast_session.elapsed_time or 0
        time_secs = int(elapsed)

        # Map to session event names
        session_name_map = {
            "turning_point": "turning_point",
            "dry_end": "dry_end",
            "first_crack": "fc_start",
            "fc_end": "fc_end",
            "second_crack": "sc_start",
        }

        session_name = session_name_map[name]
        # Manual override: an operator mark replaces an auto-detected one.
        if session_name in state.roast_session.auto_events:
            state.roast_session.collector.clear_event(session_name)
            state.roast_session.auto_events.discard(session_name)
        try:
            state.roast_session.mark_event(
                session_name, temp=current_bt, time_seconds=float(time_secs)
            )
        except DuplicateEventError:
            return {"status": "already_marked", "event": name}
        except (InvalidStateError, TransitionNotAllowed) as e:
            return {"status": "error", "error": str(e)}

        await state.driver.trigger_event(event_map[name])

        return {
            "status": "marked",
            "event": name,
            "temp": current_bt,
            "time_seconds": time_secs,
        }

    # -- Drop ---------------------------------------------------------------

    @router.post("/drop/detected/dismiss")
    async def dismiss_detected_drop(request: Request) -> dict[str, Any]:
        state = _get_state(request)
        if state.roast_session is None or not state.roast_session.is_active:
            return {"status": "error", "error": "No active roast"}
        state.roast_session.dismiss_detected_drop()
        return {"status": "dismissed"}

    @router.post("/drop")
    async def execute_drop(request: Request, use_detected: bool = True) -> dict[str, Any]:
        return await drop_active_roast(
            _get_state(request),
            agent=_get_ai_agent(request),
            use_detected=use_detected,
        )

    # -- Cooling fan ----------------------------------------------------------

    @router.post("/cooling")
    async def toggle_cooling(request: Request, body: CoolingRequest) -> dict[str, Any]:
        state = _get_state(request)
        if state.driver is None or not state.driver.is_connected:
            return {"status": "error", "error": "Roaster not connected"}
        try:
            status = await state.cooling_fan().set_enabled(
                state.driver, body.enabled, auto=False, schedule_off=False
            )
            return {
                "status": "ok",
                "cooling": status["enabled"],
                "confirmed": status["confirmed"],
                **status,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # -- Coffee CRUD -----------------------------------------------------------

    @router.get("/coffees")
    async def list_coffees(request: Request) -> list[dict[str, Any]]:
        _get_state(request)  # validate state
        repo = CoffeeRepository()
        coffees = await repo.get_all()
        return [await coffee_api_response(c) for c in coffees]

    @router.post("/coffees")
    async def create_coffee(request: Request) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import CoffeeCreate

        data = CoffeeCreate(**body)
        repo = CoffeeRepository()
        coffee = await repo.create(data)
        return await coffee_api_response(coffee)

    @router.get("/coffees/{coffee_id}")
    async def get_coffee(request: Request, coffee_id: str) -> dict[str, Any]:
        _get_state(request)
        repo = CoffeeRepository()
        coffee = await repo.get_by_id(coffee_id)
        if coffee is None:
            return {"status": "error", "error": "Coffee not found"}
        return await coffee_api_response(coffee)

    @router.put("/coffees/{coffee_id}")
    async def update_coffee(request: Request, coffee_id: str) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import CoffeeUpdate

        data = CoffeeUpdate(**body)
        repo = CoffeeRepository()
        coffee = await repo.update(coffee_id, data)
        if coffee is None:
            return {"status": "error", "error": "Coffee not found"}
        return await coffee_api_response(coffee)

    @router.delete("/coffees/{coffee_id}")
    async def delete_coffee(
        request: Request, coffee_id: str, force: bool = False
    ) -> dict[str, Any]:
        _get_state(request)
        repo = CoffeeRepository()
        coffee = await repo.get_by_id(coffee_id)
        if coffee is None:
            return {"status": "error", "error": "Coffee not found"}
        # Check linked roasts/profiles unless force
        if not force:
            profile_repo = ProfileRepository()
            roast_repo = RoastRepository()
            profile_count = await profile_repo.count(coffee_id=coffee_id)
            # Filter roast count by coffee_id
            roast_count_full = len(await roast_repo.get_by_coffee_id(coffee_id, limit=10000))
            if profile_count > 0 or roast_count_full > 0:
                return {
                    "status": "error",
                    "error": f"Coffee has {profile_count} profiles and {roast_count_full} roasts. Use ?force=true to delete.",
                    "profiles": profile_count,
                    "roasts": roast_count_full,
                }
        await repo.delete(coffee_id)
        return {"status": "deleted", "id": coffee_id}

    # -- Profile CRUD ----------------------------------------------------------

    @router.get("/profiles")
    async def list_profiles(request: Request) -> list[dict[str, Any]]:
        _get_state(request)
        repo = ProfileRepository()
        profiles = await repo.get_all()
        return [profile_to_response(p).model_dump() for p in profiles]

    @router.post("/profiles")
    async def create_profile(request: Request) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import ProfileCreate

        data = ProfileCreate(**body)
        repo = ProfileRepository()
        profile = await repo.create(data)
        return profile_to_response(profile).model_dump()

    @router.get("/profiles/{profile_id}")
    async def get_profile(request: Request, profile_id: str) -> dict[str, Any]:
        _get_state(request)
        repo = ProfileRepository()
        profile = await repo.get_by_id(profile_id)
        if profile is None:
            return {"status": "error", "error": "Profile not found"}
        return profile_to_response(profile).model_dump()

    @router.put("/profiles/{profile_id}")
    async def update_profile(request: Request, profile_id: str) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import ProfileUpdate

        data = ProfileUpdate(**body)
        repo = ProfileRepository()
        profile = await repo.update(profile_id, data)
        if profile is None:
            return {"status": "error", "error": "Profile not found"}
        return profile_to_response(profile).model_dump()

    @router.delete("/profiles/{profile_id}")
    async def delete_profile(request: Request, profile_id: str) -> dict[str, Any]:
        _get_state(request)
        repo = ProfileRepository()
        deleted = await repo.delete(profile_id)
        if not deleted:
            return {"status": "error", "error": "Profile not found"}
        return {"status": "deleted", "id": profile_id}

    # -- Roast History ---------------------------------------------------------

    @router.get("/roasts")
    async def list_roasts(request: Request) -> list[dict[str, Any]]:
        from maillard.db.aggregation import aggregate_cuppings

        _get_state(request)
        roast_repo = RoastRepository()
        coffee_repo = CoffeeRepository()
        profile_repo = ProfileRepository()
        cupping_repo = CuppingRepository()

        roasts = await roast_repo.get_all_with_relations()
        results = []
        for r in roasts:
            d = roast_to_response(r).model_dump()
            # Load related entities separately
            coffee = await coffee_repo.get_by_id(r.coffee_id) if r.coffee_id else None
            profile = await profile_repo.get_by_id(r.profile_id) if r.profile_id else None
            cuppings = await cupping_repo.get_by_roast_id(r.id)
            d["coffee_name"] = coffee.name if coffee else None
            d["profile_name"] = profile.name if profile else None
            agg = aggregate_cuppings(cuppings)
            d["cupping_avg_total"] = agg.avg_total
            d["cupping_count"] = agg.count
            results.append(d)
        return results

    @router.get("/roasts/compare")
    async def compare_roasts(request: Request, ids: str = "") -> dict[str, Any]:
        """Side-by-side comparison of 2-10 roasts by key metrics."""
        from maillard.db.aggregation import aggregate_cuppings

        _get_state(request)
        id_list = [i.strip() for i in ids.split(",") if i.strip()]
        if len(id_list) < 2:
            return {"status": "error", "error": "Provide at least 2 roast IDs (ids=uuid1,uuid2)"}
        if len(id_list) > 10:
            return {"status": "error", "error": "Maximum 10 roast IDs"}

        roast_repo = RoastRepository()
        coffee_repo = CoffeeRepository()
        cupping_repo = CuppingRepository()
        results = []
        for rid in id_list:
            roast = await roast_repo.get_by_id(rid)
            if roast is None:
                return {"status": "error", "error": f"Roast not found: {rid}"}
            coffee = await coffee_repo.get_by_id(roast.coffee_id) if roast.coffee_id else None
            cuppings = await cupping_repo.get_by_roast_id(roast.id)
            agg = aggregate_cuppings(cuppings)
            results.append(
                {
                    "roast_id": roast.id,
                    "date": roast.start_time[:10] if roast.start_time else None,
                    "coffee_name": coffee.name if coffee else None,
                    "charge_temp": roast.charge_temp,
                    "turning_point_time": roast.turning_point_time,
                    "dry_end_time": roast.dry_end_time,
                    "fc_start_time": roast.fc_start_time,
                    "fc_start_temp": roast.fc_start_temp,
                    "drop_temp": roast.drop_temp,
                    "drop_time": roast.drop_time,
                    "total_time_seconds": roast.total_time_seconds,
                    "dtr": roast.dtr,
                    "cupping_avg_total": agg.avg_total,
                    "cupping_count": agg.count,
                    "status": roast.status,
                }
            )
        return {"roasts": results}

    @router.get("/roasts/{roast_id}/similar")
    async def get_similar_roasts(request: Request, roast_id: str) -> dict[str, Any]:
        """Get up to 5 completed roasts of the same coffee (excluding self)."""
        from maillard.db.aggregation import aggregate_cuppings

        _get_state(request)
        roast_repo = RoastRepository()
        cupping_repo = CuppingRepository()

        roast = await roast_repo.get_by_id(roast_id)
        if roast is None:
            return {"status": "error", "error": "Roast not found"}
        if roast.coffee_id is None:
            return {"roasts": [], "message": "Roast has no associated coffee"}
        similar = await roast_repo.get_completed_by_coffee_id(
            roast.coffee_id, limit=5, exclude_roast_id=roast_id
        )
        results = []
        for r in similar:
            cuppings = await cupping_repo.get_by_roast_id(r.id)
            agg = aggregate_cuppings(cuppings)
            results.append(
                {
                    "roast_id": r.id,
                    "date": r.start_time[:10] if r.start_time else None,
                    "charge_temp": r.charge_temp,
                    "drop_temp": r.drop_temp,
                    "total_time_seconds": r.total_time_seconds,
                    "dtr": r.dtr,
                    "cupping_avg_total": agg.avg_total,
                    "cupping_count": agg.count,
                    "status": r.status,
                }
            )
        return {"roasts": results, "coffee_id": roast.coffee_id}

    @router.get("/roasts/{roast_id}")
    async def get_roast_detail(request: Request, roast_id: str) -> dict[str, Any]:
        _get_state(request)
        roast_repo = RoastRepository()
        coffee_repo = CoffeeRepository()
        profile_repo = ProfileRepository()
        cupping_repo = CuppingRepository()

        roast = await roast_repo.get_by_id(roast_id)
        if roast is None:
            return {"status": "error", "error": "Roast not found"}
        d = roast_to_response_with_curve(roast).model_dump()
        # Load related entities
        coffee = await coffee_repo.get_by_id(roast.coffee_id) if roast.coffee_id else None
        profile = await profile_repo.get_by_id(roast.profile_id) if roast.profile_id else None
        cuppings = await cupping_repo.get_by_roast_id(roast.id)
        d["coffee_name"] = coffee.name if coffee else None
        d["profile_name"] = profile.name if profile else None
        # Include full coffee details for labels
        if coffee:
            d["coffee"] = await coffee_api_response(coffee)
        # Include profile roast_level
        if profile:
            d["roast_level"] = profile.roast_level
        # Include cuppings
        d["cuppings"] = [cupping_to_response(c).model_dump() for c in cuppings]
        return d

    @router.patch("/roasts/{roast_id}")
    @router.put("/roasts/{roast_id}")
    async def update_roast(request: Request, roast_id: str) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import RoastUpdate

        data = RoastUpdate(**body)
        repo = RoastRepository()
        roast = await repo.update(roast_id, data)
        if roast is None:
            return {"status": "error", "error": "Roast not found"}
        if data.model_fields_set - {"excluded"}:
            _enqueue_site_sync(roast_id)
        return roast_to_response(roast).model_dump()

    @router.delete("/roasts/{roast_id}")
    async def delete_roast(request: Request, roast_id: str) -> dict[str, Any]:
        _get_state(request)
        repo = RoastRepository()
        deleted = await repo.delete(roast_id)
        if not deleted:
            return {"status": "error", "error": "Roast not found"}
        return {"status": "deleted", "id": roast_id}

    @router.post("/roasts/{roast_id}/weights")
    async def set_roast_weights(
        request: Request, roast_id: str, body: RoastWeightsRequest
    ) -> dict[str, Any]:
        """Record green/roasted weights; weight loss % is computed."""
        _get_state(request)
        from maillard.roasting.codes import compute_weight_loss

        roast = await Roast.get(roast_id)
        if roast is None:
            return {"status": "error", "error": "Roast not found"}
        updates = {
            key: value
            for key, value in body.model_dump(exclude_unset=True).items()
            if value is not None
        }
        if body.drop_id is not None:
            drop = await PresaleDrop.get(body.drop_id)
            if drop is None or drop.coffee_id != roast.coffee_id or drop.status == "cancelled":
                raise HTTPException(409, "Drop must belong to the roast coffee and be active")
        if (
            roast.green_deducted
            and roast.drop_id is not None
            and body.drop_id is not None
            and body.drop_id != roast.drop_id
        ):
            raise HTTPException(
                409, "Drop already accounted; reconcile inventory before changing it"
            )
        candidate = roast.model_copy(update=updates)
        if (
            candidate.green_weight_grams is not None
            and candidate.roasted_weight_grams is not None
            and candidate.roasted_weight_grams > candidate.green_weight_grams
        ):
            return {"status": "error", "error": "Roasted weight exceeds green weight"}
        # The operator can edit the suggested count; an existing explicit count is preserved.
        if candidate.bags_packed is None and candidate.roasted_weight_grams is not None:
            import math

            updates["bags_packed"] = math.floor(
                candidate.roasted_weight_grams / candidate.bag_size_grams
            )
        loss = compute_weight_loss(candidate.green_weight_grams, candidate.roasted_weight_grams)
        if loss is not None:
            updates["weight_loss"] = loss
        if body.drop_id is not None:
            result = await Roast.find_one({"_id": roast_id, "drop_id": roast.drop_id}).update(
                {"$set": updates}
            )
            if result.matched_count != 1:
                raise HTTPException(409, "Drop association changed concurrently; reload the roast")
            roast = await Roast.get(roast_id)
        else:
            await roast.set(updates)
        try:
            inventory = await deduct_for_roast(roast)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        _enqueue_site_sync(roast_id)
        return {
            "status": "ok",
            "green_weight_grams": roast.green_weight_grams,
            "roasted_weight_grams": roast.roasted_weight_grams,
            "weight_loss": roast.weight_loss,
            "bags_packed": roast.bags_packed,
            "bag_size_grams": roast.bag_size_grams,
            "drop_id": roast.drop_id,
            "inventory": inventory,
        }

    @router.post("/roasts/{roast_id}/ensure-code")
    async def ensure_code(request: Request, roast_id: str) -> dict[str, Any]:
        """Assign a roast code to an older roast that predates codes."""
        _get_state(request)
        from maillard.roasting.codes import ensure_roast_code

        roast = await Roast.get(roast_id)
        if roast is None:
            return {"status": "error", "error": "Roast not found"}
        return {"status": "ok", "code": await ensure_roast_code(roast)}

    @router.post("/roasts/{roast_id}/regenerate-name")
    async def regenerate_roast_name(request: Request, roast_id: str) -> dict[str, Any]:
        _get_state(request)
        from maillard.utils.naming import generate_roast_name

        repo = RoastRepository()
        roast = await repo.get_by_id(roast_id)
        if roast is None:
            return {"status": "error", "error": "Roast not found"}
        roast.roast_name = generate_roast_name()
        await roast.set({"roast_name": roast.roast_name})
        _enqueue_site_sync(roast_id)
        return {"status": "ok", "roast_name": roast.roast_name}

    # -- Cupping CRUD ----------------------------------------------------------

    @router.get("/cuppings")
    async def list_cuppings(request: Request, roast_id: str | None = None) -> list[dict[str, Any]]:
        _get_state(request)
        repo = CuppingRepository()
        if roast_id:
            cuppings = await repo.get_by_roast_id(roast_id)
        else:
            cuppings = await repo.get_all()
        return [cupping_to_response(c).model_dump() for c in cuppings]

    @router.post("/cuppings")
    async def create_cupping(request: Request) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import CuppingCreate, CuppingScores

        # Flatten scores from top-level fields into nested scores object
        scores_fields = ["aroma", "acidity", "sweetness", "body", "aftertaste", "overall"]
        scores_data = {k: body.pop(k) for k in scores_fields if k in body and body[k] is not None}
        if scores_data:
            body["scores"] = CuppingScores(**scores_data)
        data = CuppingCreate(**body)
        repo = CuppingRepository()
        cupping = await repo.create(data)
        return cupping_to_response(cupping).model_dump()

    @router.get("/cuppings/{cupping_id}")
    async def get_cupping(request: Request, cupping_id: str) -> dict[str, Any]:
        _get_state(request)
        repo = CuppingRepository()
        cupping = await repo.get_by_id(cupping_id)
        if cupping is None:
            return {"status": "error", "error": "Cupping not found"}
        return cupping_to_response(cupping).model_dump()

    @router.put("/cuppings/{cupping_id}")
    async def update_cupping(request: Request, cupping_id: str) -> dict[str, Any]:
        _get_state(request)
        body = await request.json()
        from maillard.db.schemas import CuppingScores, CuppingUpdate

        scores_fields = ["aroma", "acidity", "sweetness", "body", "aftertaste", "overall"]
        scores_data = {k: body.pop(k) for k in scores_fields if k in body and body[k] is not None}
        if scores_data:
            body["scores"] = CuppingScores(**scores_data)
        data = CuppingUpdate(**body)
        repo = CuppingRepository()
        cupping = await repo.update(cupping_id, data)
        if cupping is None:
            return {"status": "error", "error": "Cupping not found"}
        return cupping_to_response(cupping).model_dump()

    @router.delete("/cuppings/{cupping_id}")
    async def delete_cupping(request: Request, cupping_id: str) -> dict[str, Any]:
        _get_state(request)
        repo = CuppingRepository()
        deleted = await repo.delete(cupping_id)
        if not deleted:
            return {"status": "error", "error": "Cupping not found"}
        return {"status": "deleted", "id": cupping_id}

    # -- AI Agent ---------------------------------------------------------------

    @router.post("/auto-drop")
    async def set_auto_drop(request: Request, body: AutoDropRequest) -> dict[str, Any]:
        state = _get_state(request)
        state.config.auto_drop = body.enabled
        return {"status": "ok", "auto_drop": bool(state.config.auto_drop)}

    @router.post("/ai/toggle")
    async def ai_toggle(request: Request, body: AIToggleRequest) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"status": "error", "error": "AI agent not available (set ANTHROPIC_API_KEY)"}

        from maillard.ai.agent import AIMode

        mode_map = {
            "off": AIMode.OFF,
            "suggest": AIMode.SUGGEST,
            "auto": AIMode.AUTO,
        }
        mode = mode_map.get(body.mode)
        if mode is None:
            return {"status": "error", "error": f"Invalid mode: {body.mode}"}

        try:
            await agent.set_mode(mode)
        except PermissionError as e:
            return {"status": "error", "error": str(e), "mode": agent.mode.name.lower()}

        state = _get_state(request)
        if state._safety_monitor is not None:
            state._safety_monitor.set_watchdog_enforced(mode == AIMode.AUTO)
        if state.roast_context.roast_id and state.is_roast_active:
            roast = await Roast.get(state.roast_context.roast_id)
            if roast is not None:
                new_mode = _control_mode(agent)
                # Record the most autonomous mode used during the roast
                rank = {"manual": 0, "assist": 1, "auto": 2}
                if rank.get(new_mode, 0) > rank.get(roast.control_mode or "manual", 0):
                    roast.control_mode = new_mode
                    await roast.set({"control_mode": roast.control_mode})
        return {"status": "ok", "mode": agent.status_mode}

    @router.post("/ai/confirm")
    async def ai_confirm(request: Request, body: AIConfirmRequest) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"status": "error", "error": "AI agent not available"}

        suggestion = body.model_dump(exclude_none=True)
        result = await agent.apply_suggestion(suggestion)
        await agent.record_suggestion_action("accepted", suggestion, result=result)
        return {"status": "ok", **result}

    @router.post("/ai/action")
    async def ai_action(request: Request, body: AIActionRequest) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"status": "error", "error": "AI agent not available"}
        action_map = {
            "accept": "accepted",
            "accepted": "accepted",
            "reject": "rejected",
            "rejected": "rejected",
            "dismiss": "dismissed",
            "dismissed": "dismissed",
        }
        action = action_map.get(body.action)
        if action is None:
            return {"status": "error", "error": f"Invalid action: {body.action}"}
        result = await agent.record_suggestion_action(action, body.suggestion)
        if "error" in result:
            return {"status": "error", "error": result["error"]}
        return {"status": "ok", **result}

    @router.get("/ai/status")
    async def ai_status(request: Request) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {
                "available": False,
                "live_model": _get_state(request).config.ai_live_model,
                "plan_model": _get_state(request).config.ai_plan_model,
            }

        status = agent.get_status()
        status["available"] = True
        return status

    @router.get("/ai/transcript")
    async def ai_transcript(request: Request) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"available": False, "entries": []}
        return {"available": True, "entries": agent.get_transcript()}

    @router.get("/ai/prompt")
    async def ai_prompt(request: Request) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"available": False, "message": "AI agent not available"}
        return agent.get_prompt_snapshot()

    @router.post("/ai/message")
    async def ai_message(request: Request, body: AIMessageRequest) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"status": "error", "error": "AI agent not available"}
        result = await agent.add_operator_message(body.message)
        if "error" in result:
            return {"status": "error", "error": result["error"]}
        return {"status": "ok", **result}

    @router.post("/ai/context")
    async def ai_context(request: Request, body: AIContextRequest) -> dict[str, Any]:
        agent = _get_ai_agent(request)
        if agent is None:
            return {"status": "no_agent"}
        agent.set_roast_context(
            bean_name=body.bean_name,
            roast_level=body.roast_level,
            batch_grams=body.batch_grams,
        )
        return {"status": "ok"}

    @router.post("/ai/recommend")
    async def ai_recommend(request: Request) -> dict[str, Any]:
        from maillard.ai.recommend import get_recommendation

        raw = await request.json()
        _logger.debug("ai/recommend raw body: %s", raw)
        try:
            body = RecommendRequest(**raw)
        except Exception as e:
            _logger.warning("ai/recommend validation failed: %s (body=%s)", e, raw)
            return {"status": "error", "error": str(e)}

        # Get AI client from agent if available
        ai_client = None
        ai_model = None
        agent = _get_ai_agent(request)
        if agent is not None:
            ai_client = agent._client
            ai_model = agent._model
            agent.set_pending_coffee_id(body.coffee_id)

        try:
            rec = await get_recommendation(
                coffee_id=body.coffee_id,
                profile_id=body.profile_id,
                client=ai_client,
                model=ai_model,
            )
            return {
                "status": "ok",
                "recommendation": {
                    "charge_temp": rec.charge_temp,
                    "heat_percent": rec.heat_percent,
                    "fan_percent": rec.fan_percent,
                    "drum_percent": rec.drum_percent,
                    "reasoning": rec.reasoning,
                    "source": rec.source,
                    "confidence": rec.confidence,
                },
            }
        except Exception as e:
            _logger.exception("Failed to generate recommendation")
            return {"status": "error", "error": str(e)}

    # -- AI Profile Generation -------------------------------------------------

    @router.post("/ai/generate-profile")
    async def ai_generate_profile(request: Request) -> dict[str, Any]:
        from maillard.ai.generate import generate_profile

        raw = await request.json()
        _logger.debug("ai/generate-profile raw body: %s", raw)
        try:
            body = GenerateProfileRequest(**raw)
        except Exception as e:
            _logger.warning("ai/generate-profile validation failed: %s (body=%s)", e, raw)
            return {"status": "error", "error": str(e)}

        # Get AI client from agent if available
        ai_client = None
        ai_model = _get_state(request).config.ai_plan_model
        agent = _get_ai_agent(request)
        if agent is not None:
            ai_client = agent._client

        try:
            result = await generate_profile(
                coffee_id=body.coffee_id,
                roast_level=body.roast_level,
                flavor_goal=body.flavor_goal,
                brew_method=body.brew_method,
                batch_size_grams=body.batch_size_grams,
                client=ai_client,
                model=ai_model,
            )
            from maillard.roasting.plan import (
                derive_targets,
                interpolate_planned_curve,
                validate_plan,
            )

            state = _get_state(request)
            return {
                "status": "ok",
                "profile": {
                    "name": result.name,
                    "phases": result.phases,
                    "charge_temp": result.charge_temp,
                    "target_dtr": result.target_dtr,
                    "ai_rationale": result.ai_rationale,
                    "source": result.source,
                    "ai_model": result.ai_model,
                    "batch_size_grams": result.batch_size_grams,
                    "roast_level": result.roast_level,
                    "brew_method": result.brew_method,
                    "targets": derive_targets(result.phases),
                    "planned_curve": interpolate_planned_curve(result.phases),
                    "warnings": list(
                        dict.fromkeys(
                            [
                                *result.warnings,
                                *validate_plan(
                                    result.phases,
                                    charge_temp=result.charge_temp,
                                    batch_size_grams=result.batch_size_grams,
                                    target_dtr=result.target_dtr,
                                    max_bt_temp=state.config.max_bt_temp,
                                ),
                            ]
                        )
                    ),
                },
            }
        except Exception as e:
            _logger.exception("Failed to generate profile")
            return {"status": "error", "error": str(e)}

    # -- Label Printing --------------------------------------------------------

    def _get_label_themes(request: Request) -> dict:
        """Load label themes (cached on app state)."""
        from maillard.web.label_styles import load_themes

        state = _get_state(request)
        # Cache themes on app state to avoid re-reading YAML every request
        if not hasattr(state, "_label_themes") or state._label_themes is None:
            state._label_themes = load_themes(
                state.config.label_styles_dir, state.config.label_brand_name
            )
        return state._label_themes

    def _get_label_theme(request: Request, style_id: str | None = None):
        """Get a specific label theme by ID."""
        from maillard.web.label_styles import get_theme

        state = _get_state(request)
        themes = _get_label_themes(request)
        effective_style = style_id or state.config.label_default_style
        return get_theme(effective_style, themes)

    async def _build_label_data(request: Request, roast_id: str) -> dict[str, Any] | None:
        """Load roast + related data and build a LabelData dict. Returns None on 404."""
        roast_repo = RoastRepository()
        coffee_repo = CoffeeRepository()
        profile_repo = ProfileRepository()

        roast = await roast_repo.get_by_id(roast_id)
        if roast is None:
            return None

        coffee = await coffee_repo.get_by_id(roast.coffee_id) if roast.coffee_id else None
        profile = await profile_repo.get_by_id(roast.profile_id) if roast.profile_id else None

        state = _get_state(request)
        from urllib.parse import urlsplit

        from maillard.roasting.codes import ensure_roast_code

        code = await ensure_roast_code(roast)
        base_url = state.config.site_url.rstrip("/")
        if state.config.label_base_url:
            _logger.warning("MAILLARD_LABEL_BASE_URL is deprecated; use MAILLARD_SITE_URL")
            base_url = state.config.label_base_url.rstrip("/")
        qr_url = f"{base_url}/r/{code}"
        host = urlsplit(qr_url).netloc.removeprefix("www.")
        short_url = f"{host}{urlsplit(qr_url).path}"
        sync_ok = bool(
            roast.site_synced_at and roast.site_synced_code == code and roast.site_published is True
        )

        roast_date = None
        if roast.start_time:
            try:
                roast_date = datetime.fromisoformat(roast.start_time).strftime("%B %d, %Y")
            except (ValueError, TypeError):
                roast_date = roast.start_time[:10]

        total_time_str = None
        if roast.total_time_seconds:
            mins, secs = divmod(roast.total_time_seconds, 60)
            total_time_str = f"{mins}:{secs:02d}"

        varietal_str = None
        if coffee and coffee.varietal:
            varietal_str = (
                ", ".join(coffee.varietal)
                if isinstance(coffee.varietal, list)
                else str(coffee.varietal)
            )

        from maillard.web.label_service import LabelData

        # Bond-specific fields (stored on roast model)
        bond_number = roast.bond_number
        cupping_score = roast.cupping_score
        flavor_notes = roast.flavor_notes
        if isinstance(flavor_notes, list):
            flavor_notes = ", ".join(flavor_notes)
        weight_display = roast.weight_display

        if bond_number is not None and weight_display is None:
            weight_display = "12oz / 340g"

        return {
            "roast": roast,
            "sync_ok": sync_ok,
            "qr_url": qr_url,
            "label_data": LabelData(
                roast_id=roast_id,
                coffee_name=coffee.name if coffee else roast.coffee_id or "Single Origin",
                origin=coffee.origin if coffee else None,
                processing=coffee.processing if coffee else None,
                varietal=varietal_str,
                altitude=coffee.altitude if coffee else None,
                roast_level=profile.roast_level if profile else None,
                roast_date=roast_date,
                drop_temp=roast.drop_temp,
                total_time=total_time_str,
                dtr=roast.dtr,
                batch_size_grams=roast.batch_size_grams,
                qr_url=qr_url,
                roast_code=code,
                short_url=short_url,
                bond_number=bond_number,
                cupping_score=cupping_score,
                flavor_notes=flavor_notes,
                weight_display=weight_display,
                roast_name=roast.roast_name,
            ),
        }

    @router.get("/label-styles")
    async def list_label_styles(request: Request) -> list[dict[str, str]]:
        state = _get_state(request)
        themes = _get_label_themes(request)
        site_host = _site_host(state.config.site_url)
        return [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "layout": t.layout,
                "brand_name": t.brand_name,
                "site_host": site_host,
            }
            for t in themes.values()
        ]

    def _apply_label_overrides(data, overrides):
        """Apply request-only text edits without changing stored coffee data."""
        if isinstance(overrides, dict):
            for key in ("coffee_name", "origin"):
                value = overrides.get(key)
                if isinstance(value, str) and value.strip():
                    setattr(data, key, value.strip()[:120])
        return data

    @router.post("/labels/preview")
    async def label_preview(request: Request) -> dict[str, Any]:
        import base64

        from maillard.web.label_service import build_preview_png

        body = await request.json()
        roast_id = body.get("roast_id")
        if not roast_id:
            return {"status": "error", "error": "roast_id is required"}

        result = await _build_label_data(request, roast_id)
        if result is None:
            return {"status": "error", "error": "Roast not found"}

        _apply_label_overrides(result["label_data"], body.get("overrides"))
        theme = _get_label_theme(request, body.get("style"))
        png_bytes = build_preview_png(result["label_data"], theme=theme)
        b64 = base64.b64encode(png_bytes).decode("ascii")
        return {
            "status": "ok",
            "image": f"data:image/png;base64,{b64}",
            "sync_ok": result["sync_ok"],
            "qr_url": result["qr_url"],
        }

    @router.post("/labels/print")
    async def label_print(request: Request) -> dict[str, Any]:
        from maillard.web.label_service import (
            build_preview_png,
            build_zpl,
            print_via_cups,
            send_to_printer,
        )

        state = _get_state(request)
        body = await request.json()
        roast_id = body.get("roast_id")
        if not roast_id:
            return {"status": "error", "error": "roast_id is required"}

        result = await _build_label_data(request, roast_id)
        if result is None:
            return {"status": "error", "error": "Roast not found"}

        roast = result["roast"]
        from maillard.web.label_service import require_packed_weights

        require_packed_weights(roast)
        if body.get("override") is True:
            _logger.warning("Printing label with readiness override for roast %s", roast_id)
        elif not roast.site_synced_at or roast.site_synced_code != roast.code:
            return {"status": "error", "error": "Sync to site first", "code": "sync_required"}
        elif roast.site_published is not True:
            return {
                "status": "error",
                "error": f"Roast is not published: {roast.site_warnings}",
                "code": "not_published",
                "warnings": roast.site_warnings,
            }
        _apply_label_overrides(result["label_data"], body.get("overrides"))
        theme = _get_label_theme(request, body.get("style"))
        transport = _label_transport(state.config)
        if transport is None:
            return {"status": "error", "error": "Printer not configured"}
        fallback = False
        try:
            if transport == "cups":
                try:
                    print_via_cups(
                        build_preview_png(result["label_data"], theme=theme),
                        state.config.zebra_cups_name,
                        darkness=state.config.zebra_cups_darkness,
                        label_width=theme.width_inches,
                        label_height=theme.height_inches,
                    )
                except FileNotFoundError:
                    if state.config.zebra_transport != "auto" or not state.config.zebra_host:
                        raise
                    _logger.warning("lp unavailable; falling back to TCP label printing")
                    transport = "tcp"
                    fallback = True
            if transport == "tcp":
                send_to_printer(
                    build_zpl(result["label_data"], theme=theme),
                    state.config.zebra_host,
                    state.config.zebra_port,
                )
        except Exception as e:
            _logger.exception("Failed to send label to printer")
            return {"status": "error", "error": f"Printer error: {e}"}
        return {
            "status": "ok",
            "message": "Label sent to printer"
            + (" (lp unavailable; fell back to TCP)" if fallback else ""),
            "printer": transport,
            "fallback": fallback,
        }

    def _label_transport(config):
        if config.zebra_transport == "tcp":
            return "tcp" if config.zebra_host else None
        if config.zebra_transport == "cups":
            return "cups" if config.zebra_cups_name else None
        return "cups" if config.zebra_cups_name else "tcp" if config.zebra_host else None

    @router.get("/labels/print-readiness/{roast_id}")
    async def label_print_readiness(request: Request, roast_id: str) -> dict[str, Any]:
        result = await _build_label_data(request, roast_id)
        if result is None:
            return {"status": "error", "error": "Roast not found"}
        return {
            "weights_ok": result["roast"].roasted_weight_grams is not None
            and result["roast"].bags_packed is not None,
            "sync_ok": result["sync_ok"],
            "qr_url": result["qr_url"],
            "code": result["roast"].code,
            "published": result["roast"].site_published,
            "warnings": result["roast"].site_warnings,
            "site_sync_status": result["roast"].site_sync_status,
            "printer": _label_transport(_get_state(request).config),
        }

    def site_client(request: Request):
        from maillard.sync.site import SiteSyncClient

        client = getattr(request.app.state, "site_sync_client", None)
        if client is None:
            client = SiteSyncClient(_get_state(request).config)
            request.app.state.site_sync_client = client
        return client

    @router.post("/roasts/{roast_id}/sync")
    async def sync_roast_endpoint(request: Request, roast_id: str) -> dict[str, Any]:
        from maillard.sync.site import sync_roast_now

        return await sync_roast_now(roast_id, site_client(request))

    @router.get("/roasts/{roast_id}/sync")
    async def roast_sync_state(request: Request, roast_id: str) -> dict[str, Any]:
        from maillard.db.models import SyncOutbox

        roast = await Roast.get(roast_id)
        item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast_id)
        return {
            "configured": site_client(request).is_configured,
            "outbox": item.model_dump(mode="json") if item else None,
            "sync_status": roast.site_sync_status if roast else None,
            "url": roast.site_url if roast else None,
            "synced_at": roast.site_synced_at if roast else None,
            "synced_code": roast.site_synced_code if roast else None,
            "code": roast.code if roast else None,
        }

    @router.get("/sync/status")
    async def sync_status(request: Request) -> dict[str, Any]:
        from maillard.db.models import SyncOutbox

        client = site_client(request)
        counts = {
            status: await SyncOutbox.find(SyncOutbox.status == status).count()
            for status in ("pending", "failed", "synced")
        }
        recent = await SyncOutbox.find_all().sort("-updated_at").limit(20).to_list()
        return {
            "configured": client.is_configured,
            "site_url": client.config.site_url,
            **counts,
            "recent": [item.model_dump(mode="json") for item in recent],
        }

    @router.get("/qr/{coffee_slug}")
    async def generate_qr(request: Request, coffee_slug: str) -> Any:
        """Generate a QR code PNG for a published coffee product.

        QR links to: <MAILLARD_SITE_URL>/coffees/<slug>
        """
        import io

        import qrcode
        from fastapi.responses import StreamingResponse

        state = _get_state(request)

        url = f"{state.config.site_url.rstrip('/')}/coffees/{coffee_slug}"
        qr = qrcode.QRCode(version=None, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        return StreamingResponse(buf, media_type="image/png")

    from maillard.web.inventory_routes import router as inventory_router

    router.include_router(inventory_router)
    return router
