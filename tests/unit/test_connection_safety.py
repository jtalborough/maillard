"""Connection failure safety checks using queues, mocks and the simulator only."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from maillard.core.config import MaillardConfig
from maillard.core.events import EventBus, SafetyAlertEvent, SafetyAlertLevel
from maillard.core.heating import HeatingManager
from maillard.core.safety import SafetyMonitor
from maillard.driver.kaleido import KaleidoDriver
from maillard.driver.simulator import SimulatorDriver
from maillard.mcp.monitoring import RoastMonitor
from maillard.mcp.state import ApplicationState
from maillard.web.routes import create_router
from maillard.web.websocket import _serialize_event


def session():
    return SimpleNamespace(elapsed_time=None, current_phase=None, latest_ror=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, ConnectionError("offline")])
async def test_disconnected_web_estop_halts_locals_and_attempts_driver(failure):
    bus = EventBus()
    events = []
    bus.on(SafetyAlertEvent, events.append)
    driver = SimpleNamespace(
        is_connected=False,
        emergency_stop=AsyncMock(return_value=False),
        set_heating_state=AsyncMock(side_effect=ConnectionError("offline")),
    )
    if isinstance(failure, Exception):
        driver.emergency_stop.side_effect = failure
    heating = SimpleNamespace(stop=AsyncMock())
    engine = SimpleNamespace(stop=AsyncMock())
    state = ApplicationState(driver=driver, event_bus=bus)
    state._heating_manager = heating
    state._execution_engine = engine
    agent = SimpleNamespace(set_mode=AsyncMock())
    app = FastAPI()
    app.state.maillard = state
    app.state.ai_agent = agent
    app.include_router(create_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.post("/api/emergency-stop")).json()
    heating.stop.assert_awaited_once()
    engine.stop.assert_awaited_once()
    agent.set_mode.assert_awaited_once()
    driver.emergency_stop.assert_awaited_once()
    driver.set_heating_state.assert_awaited_once_with(False)
    assert result["confirmed"] is False
    assert result["status"] == "emergency_stop_unconfirmed"
    assert "Local heat sources halted" in result["message"]
    assert "Kaleido controls" in result["message"]
    await asyncio.sleep(0)
    assert events[0].level == SafetyAlertLevel.CRITICAL


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, None, ConnectionError("queue failed")])
async def test_safety_estop_failure_never_claims_executed(result):
    driver = SimpleNamespace(emergency_stop=AsyncMock(return_value=result))
    if isinstance(result, Exception):
        driver.emergency_stop.side_effect = result
    bus = EventBus()
    events = []
    bus.on(SafetyAlertEvent, events.append)
    monitor = SafetyMonitor(driver, bus, session(), MaillardConfig())
    await monitor._trigger_emergency("test", "Overtemperature")
    await asyncio.sleep(0)
    assert events[0].level == SafetyAlertLevel.CRITICAL
    assert events[0].action_taken == "EMERGENCY STOP COMMAND FAILED - CUT HEAT ON ROASTER"
    assert "executed" not in events[0].message


@pytest.mark.asyncio
async def test_queue_timeout_raises_and_estop_attempts_all_commands():
    driver = KaleidoDriver()
    driver._write_queue = asyncio.Queue(maxsize=1)
    await driver._write_queue.put("occupied")
    with pytest.raises(ConnectionError, match="queue timeout"):
        await driver._send_message("HP", "0", timeout=0.001)
    driver._send_message = AsyncMock(side_effect=ConnectionError("queue failed"))
    assert await driver.emergency_stop() is False
    assert [c.args[0] for c in driver._send_message.call_args_list] == ["HP", "HS", "FC", "RD"]


@pytest.mark.asyncio
async def test_estop_requires_fresh_hp_zero():
    driver = KaleidoDriver()
    driver._is_connected = True
    driver._state = {"HP": 0}
    driver._last_hp_sample_time = time.monotonic() - 60

    async def send(tag, _value, **_kwargs):
        if tag == "RD":
            await driver._set_state("HP", "0", False)

    driver._send_message = AsyncMock(side_effect=send)
    assert await driver.emergency_stop() is True
    # A cached zero without a new sample is not confirmation.
    driver._last_hp_sample_time = time.monotonic() - 60
    driver._send_message = AsyncMock()
    assert await driver.emergency_stop() is False


@pytest.mark.asyncio
async def test_stale_bt_alarm_raise_clear_and_websocket_fields():
    driver = SimpleNamespace(is_connected=True, read_state=AsyncMock(return_value={"BT": 180}))
    bus = EventBus()
    events = []
    bus.on(SafetyAlertEvent, events.append)
    monitor = RoastMonitor(driver, bus, session(), alarm_clear_seconds=0)
    monitor._last_valid_bt = time.monotonic() - 16
    await monitor._check_freshness()
    await monitor._check_freshness()  # deduplicate
    await monitor._poll_once()
    await asyncio.sleep(0)
    assert len(events) == 2
    assert events[0].condition == "connection_lost" and not events[0].cleared
    assert events[1].cleared
    assert '"cleared": true' in _serialize_event(events[1])
    driver.is_connected = False
    driver.reconnect_failures = 1
    await monitor._check_freshness()
    assert monitor._connection_alarm


@pytest.mark.asyncio
async def test_cached_bt_does_not_clear_stale_alarm():
    driver = SimpleNamespace(
        is_connected=True, last_bt_sample_time=time.monotonic() - 16,
        read_state=AsyncMock(return_value={"BT": 180}),
    )
    monitor = RoastMonitor(driver, EventBus(), session(), alarm_clear_seconds=0)
    await monitor._poll_once()
    assert monitor._connection_alarm
    driver.last_bt_sample_time = time.monotonic()
    await monitor._poll_once()
    assert not monitor._connection_alarm


@pytest.mark.asyncio
async def test_monitor_survives_six_errors_and_recovers():
    driver = SimpleNamespace(
        is_connected=True,
        read_state=AsyncMock(side_effect=[ConnectionError("read failed")] * 6 + [{"BT": 180}]),
    )
    bus = EventBus()
    events = []
    bus.on(SafetyAlertEvent, events.append)
    monitor = RoastMonitor(driver, bus, session(), poll_interval=0.001, alarm_clear_seconds=0)
    try:
        await monitor.start()
        for _ in range(100):
            if driver.read_state.call_count >= 7:
                break
            await asyncio.sleep(0.001)
        assert driver.read_state.call_count >= 7
        assert monitor.is_running
        assert not monitor._connection_alarm
        assert any(not e.cleared for e in events)
        assert any(e.cleared for e in events)
    finally:
        await monitor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("missed_disconnect", [False, True])
async def test_simulator_reconnect_cannot_resume_heat(missed_disconnect):
    driver = SimulatorDriver()
    await driver.connect("simulator")
    driver.set_heat = AsyncMock(wraps=driver.set_heat)
    driver.set_heating_state = AsyncMock(wraps=driver.set_heating_state)
    manager = HeatingManager(driver, send_interval=0.01)
    try:
        await manager.start(70)
        await manager._send_once()
        await driver.disconnect()
        if not missed_disconnect:
            await manager._send_once()
        await driver.connect("simulator")
        driver.set_heat.reset_mock()
        driver.set_heating_state.reset_mock()
        await manager._send_once()
        await asyncio.sleep(0.03)
        assert manager.get_status()["state"] == "paused_disconnected"
        assert manager.heat_percent == 70
        driver.set_heat.assert_not_awaited()
        driver.set_heating_state.assert_not_awaited()
        await manager.start(50)  # explicit operator action
        await manager._send_once()
        driver.set_heating_state.assert_awaited_with(True)
        driver.set_heat.assert_awaited_with(50)
    finally:
        await manager.stop()
        await driver.disconnect()


@pytest.mark.asyncio
async def test_preheat_timeout_halts_simulator_and_publishes_alarm():
    driver = SimulatorDriver()
    await driver.connect("simulator")
    bus = EventBus()
    events = []
    bus.on(SafetyAlertEvent, events.append)
    manager = HeatingManager(driver, event_bus=bus, send_interval=0.001)
    try:
        await manager.start(80, preheat_timeout=0.01)
        await asyncio.sleep(0.04)
        assert not manager.is_running
        assert driver.state["HP"] == 0
        assert driver.state["HS"] == 0
        assert any(e.condition == "preheat_timeout" and not e.cleared for e in events)
    finally:
        await manager.stop()
        await driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("preheat", {"target_temp": 99}), ("preheat", {"target_temp": 261}),
    ("preheat", {"target_temp": 200, "heat_percent": 101}),
    ("preheat", {"target_temp": 200, "fan_percent": -1}),
    ("preheat", {"target_temp": 200, "drum_percent": 101}),
    ("controls", {"heat": -1}), ("controls", {"fan": 101}),
    ("controls", {"drum": 101}),
])
async def test_api_control_bounds_return_422(path, body):
    app = FastAPI()
    app.state.maillard = ApplicationState()
    app.include_router(create_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/{path}", json=body)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_type", [KaleidoDriver, SimulatorDriver])
@pytest.mark.parametrize("temp", [99, 261, float("nan"), float("inf")])
async def test_driver_target_temperature_bounds(driver_type, temp):
    driver = driver_type()
    if isinstance(driver, SimulatorDriver):
        await driver.connect("simulator")
    try:
        with pytest.raises(ValueError, match="100-260"):
            await driver.set_target_temp(temp)
    finally:
        if isinstance(driver, SimulatorDriver):
            await driver.disconnect()


@pytest.mark.asyncio
async def test_reconnection_alone_does_not_clear_data_alarm():
    driver = SimpleNamespace(is_connected=False, reconnect_failures=1, read_state=AsyncMock(return_value={}))
    monitor = RoastMonitor(driver, EventBus(), session(), alarm_clear_seconds=0)
    await monitor._check_freshness()
    assert monitor.connection_lost
    driver.is_connected = True
    driver.reconnect_failures = 0
    await monitor._poll_once()
    assert monitor.connection_lost
    driver.read_state.return_value = {"BT": 180}
    await monitor._poll_once()
    assert not monitor.connection_lost


@pytest.mark.asyncio
async def test_freshness_alarm_runs_while_driver_read_is_blocked():
    blocked = asyncio.Event()
    driver = SimpleNamespace(is_connected=True, read_state=blocked.wait)
    monitor = RoastMonitor(driver, EventBus(), session(), alarm_clear_seconds=0)
    try:
        await monitor.start()
        monitor._last_valid_bt = time.monotonic() - 16
        await asyncio.sleep(0.01)
        assert monitor.connection_lost
        assert monitor.is_running
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_disconnect_between_hs_and_hp_does_not_apply_old_heat():
    driver = SimpleNamespace(is_connected=True, connection_generation=0)

    async def send(tag, _value):
        if tag == 'HS':
            # A reconnect completed while the HS send yielded.
            driver.connection_generation += 1

    driver._send_message = AsyncMock(side_effect=send)
    manager = HeatingManager(driver)
    manager._heat_percent = 80
    await manager._send_once()
    driver._send_message.assert_awaited_once_with('HS', '1')
    assert manager.get_status()['state'] == 'paused_disconnected'
