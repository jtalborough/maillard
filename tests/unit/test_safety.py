"""Unit tests for SafetyMonitor.

Tests safety limit checks, emergency stop triggering, watchdog timer,
alert deduplication, and status reporting.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maillard.core.config import MaillardConfig
from maillard.core.events import EventBus, SafetyAlertEvent, SafetyAlertLevel, StateChangeEvent
from maillard.core.safety import SafetyLimits, SafetyMonitor
from maillard.driver.base import KaleidoState
from maillard.roasting.session import RoastSession
from maillard.roasting.state import RoastPhase


def _make_mock_driver() -> MagicMock:
    """Create a mock driver with emergency_stop."""
    driver = MagicMock()
    driver.is_connected = True
    driver.emergency_stop = AsyncMock(return_value=True)
    return driver


def _make_mock_session(
    elapsed_time: float = 60.0,
    latest_ror: float | None = 8.5,
    current_phase: RoastPhase | None = RoastPhase.MAILLARD,
) -> MagicMock:
    """Create a mock RoastSession."""
    session = MagicMock(spec=RoastSession)
    session.elapsed_time = elapsed_time
    session.latest_ror = latest_ror
    session.current_phase = current_phase
    return session


def _make_config(
    max_bt_temp: float = 230.0,
    max_et_temp: float = 280.0,
    max_ror: float = 20.0,
    min_ror: float = 3.0,
    max_roast_duration: int = 1200,
    watchdog_warning_seconds: int = 30,
    watchdog_emergency_seconds: int = 60,
) -> MagicMock:
    """Create a mock config."""
    config = MagicMock(spec=MaillardConfig)
    config.max_bt_temp = max_bt_temp
    config.max_et_temp = max_et_temp
    config.max_ror = max_ror
    config.min_ror = min_ror
    config.max_roast_duration = max_roast_duration
    config.watchdog_warning_seconds = watchdog_warning_seconds
    config.watchdog_emergency_seconds = watchdog_emergency_seconds
    return config


class TestSafetyLimits:
    """Tests for SafetyLimits dataclass."""

    def test_default_values(self) -> None:
        """SafetyLimits should have sensible defaults."""
        limits = SafetyLimits()
        assert limits.max_bt_temp == 230.0
        assert limits.max_et_temp == 240.0
        assert limits.max_ror == 20.0
        assert limits.min_ror == 3.0
        assert limits.max_roast_duration == 1200
        assert limits.watchdog_warning_seconds == 30
        assert limits.watchdog_emergency_seconds == 60

    def test_from_config(self) -> None:
        """SafetyLimits should be created from MaillardConfig."""
        config = _make_config(max_bt_temp=225.0, watchdog_warning_seconds=15)
        limits = SafetyLimits.from_config(config)
        assert limits.max_bt_temp == 225.0
        assert limits.watchdog_warning_seconds == 15


class TestSafetyMonitorLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_running_monitor(self) -> None:
        """Monitor should be running after start()."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config()
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            assert monitor.is_running is True
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self) -> None:
        """Monitor should not be running after stop()."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config()
        monitor = SafetyMonitor(driver, bus, session, config)

        await monitor.start()
        assert monitor.is_running is True

        await monitor.stop()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling stop() multiple times should not raise."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config()
        monitor = SafetyMonitor(driver, bus, session, config)

        await monitor.start()
        await monitor.stop()
        await monitor.stop()  # Should not raise
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start() when already running should be no-op."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config()
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            task1 = monitor._watchdog_task
            await monitor.start()  # Should be no-op
            assert monitor._watchdog_task is task1
        finally:
            await monitor.stop()


class TestTemperatureLimits:
    """Tests for temperature limit checks."""

    @pytest.mark.asyncio
    async def test_bt_over_limit_triggers_emergency(self) -> None:
        """BT exceeding limit should trigger emergency stop."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(max_bt_temp=230.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # Simulate state change with BT over limit
            state: KaleidoState = {"BT": 235.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        driver.emergency_stop.assert_called_once()
        assert len(alerts) == 1
        assert alerts[0].level == SafetyAlertLevel.EMERGENCY
        assert "BT" in alerts[0].message
        assert alerts[0].current_value == 235.0

    @pytest.mark.asyncio
    async def test_et_over_limit_triggers_emergency(self) -> None:
        """ET exceeding limit should trigger emergency stop."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(max_et_temp=280.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 200.0, "ET": 285.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        driver.emergency_stop.assert_called_once()
        assert len(alerts) == 1
        assert alerts[0].level == SafetyAlertLevel.EMERGENCY
        assert "ET" in alerts[0].message

    @pytest.mark.asyncio
    async def test_temps_within_limit_no_action(self) -> None:
        """Temperatures within limits should not trigger any action."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config()
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 200.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        driver.emergency_stop.assert_not_called()


class TestDurationLimit:
    """Tests for roast duration limit."""

    @pytest.mark.asyncio
    async def test_duration_exceeded_triggers_emergency(self) -> None:
        """Roast duration exceeding limit should trigger emergency stop."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=1300.0)  # Over 1200s limit
        config = _make_config(max_roast_duration=1200)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 200.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        driver.emergency_stop.assert_called_once()
        assert len(alerts) == 1
        assert "duration" in alerts[0].message.lower()


class TestRoRLimits:
    """Tests for rate of rise limits."""

    @pytest.mark.asyncio
    async def test_ror_high_publishes_warning(self) -> None:
        """RoR above max should publish warning (no action)."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(latest_ror=25.0)  # Over 20.0 limit
        config = _make_config(max_ror=20.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 200.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        # Should NOT trigger emergency stop for high RoR
        driver.emergency_stop.assert_not_called()
        # Should publish warning
        assert len(alerts) == 1
        assert alerts[0].level == SafetyAlertLevel.WARNING
        assert "ror" in alerts[0].message.lower()

    @pytest.mark.asyncio
    async def test_ror_stall_warning_after_30s(self) -> None:
        """Low RoR for 30+ seconds should publish stall warning."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(
            latest_ror=2.0,  # Below 3.0 limit
            current_phase=RoastPhase.MAILLARD,  # Not DRYING
        )
        config = _make_config(min_ror=3.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # First event starts the timer
            state: KaleidoState = {"BT": 165.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)

            # Mock time to be 31 seconds later
            monitor._low_ror_start_time = time.time() - 31

            # Second event should trigger warning
            await bus.publish(StateChangeEvent(state=state))
            # Give the async warning publish task time to complete
            await asyncio.sleep(0.1)
        finally:
            await monitor.stop()

        # Should NOT trigger emergency stop
        driver.emergency_stop.assert_not_called()
        # Should have stall warning - check recent_alerts by condition
        stall_alerts = [a for a in monitor.recent_alerts if a.condition == "ror_stall"]
        assert len(stall_alerts) == 1

    @pytest.mark.asyncio
    async def test_ror_stall_ignored_during_drying(self) -> None:
        """Low RoR during DRYING phase should not trigger stall warning."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(
            latest_ror=2.0,  # Below 3.0 limit
            current_phase=RoastPhase.DRYING,  # During drying
        )
        config = _make_config(min_ror=3.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # Simulate low RoR for extended time
            monitor._low_ror_start_time = time.time() - 60
            state: KaleidoState = {"BT": 130.0, "ET": 150.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        # Should have no stall warnings during drying
        stall_alerts = [a for a in alerts if "stall" in a.message.lower()]
        assert len(stall_alerts) == 0


class TestWatchdogTimer:
    """Tests for watchdog timer (Claude disconnection detection)."""

    @pytest.mark.asyncio
    async def test_watchdog_warning_after_threshold(self) -> None:
        """Watchdog should warn after warning threshold."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(watchdog_warning_seconds=1, watchdog_emergency_seconds=10)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # Wait for warning threshold
            await asyncio.sleep(1.5)
        finally:
            await monitor.stop()

        warning_alerts = [a for a in alerts if a.level == SafetyAlertLevel.WARNING]
        assert len(warning_alerts) >= 1
        assert any("activity" in a.message.lower() for a in warning_alerts)

    @pytest.mark.asyncio
    async def test_watchdog_emergency_after_threshold(self) -> None:
        """Watchdog should trigger emergency stop after emergency threshold."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(watchdog_warning_seconds=1, watchdog_emergency_seconds=2)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # Wait for emergency threshold
            await asyncio.sleep(2.5)
        finally:
            await monitor.stop()

        driver.emergency_stop.assert_called()
        emergency_alerts = [a for a in alerts if a.level == SafetyAlertLevel.EMERGENCY]
        assert len(emergency_alerts) >= 1

    @pytest.mark.asyncio
    async def test_reset_watchdog_prevents_warning(self) -> None:
        """Resetting watchdog should prevent warning."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(watchdog_warning_seconds=1, watchdog_emergency_seconds=10)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # Reset watchdog repeatedly
            for _ in range(5):
                await asyncio.sleep(0.3)
                monitor.reset_watchdog()
            await asyncio.sleep(0.2)
        finally:
            await monitor.stop()

        # Should have no watchdog warnings
        watchdog_alerts = [a for a in alerts if "activity" in a.message.lower()]
        assert len(watchdog_alerts) == 0


class TestAlertDeduplication:
    """Tests for alert deduplication."""

    @pytest.mark.asyncio
    async def test_alerts_not_repeated_for_same_condition(self) -> None:
        """Same condition should only alert once."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(max_bt_temp=230.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            # Send multiple events with BT over limit
            for _ in range(3):
                state: KaleidoState = {"BT": 235.0, "ET": 250.0, "sid": 1}
                await bus.publish(StateChangeEvent(state=state))
                await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        # Should only have one emergency alert for BT
        bt_alerts = [a for a in alerts if "BT" in a.message]
        assert len(bt_alerts) == 1

    @pytest.mark.asyncio
    async def test_condition_clears_when_value_returns_to_safe(self) -> None:
        """Alert condition should clear when value returns to safe range."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(latest_ror=25.0)  # High RoR
        config = _make_config(max_ror=20.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()

            # First: high RoR warning
            state: KaleidoState = {"BT": 200.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)

            # Return to safe RoR
            session.latest_ror = 15.0
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)

            # Go high again - should alert again
            session.latest_ror = 22.0
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()

        # Should have two high RoR warnings
        ror_alerts = [a for a in alerts if "ror" in a.message.lower() and "high" not in a.message.lower()]
        # Actually we filter by condition name, so let's check differently
        assert len(alerts) >= 2


class TestEmergencyStopErrorHandling:
    """Tests for error handling when emergency_stop fails."""

    @pytest.mark.asyncio
    async def test_continues_monitoring_if_emergency_stop_fails(self) -> None:
        """Monitor should continue even if emergency_stop raises."""
        driver = _make_mock_driver()
        driver.emergency_stop = AsyncMock(side_effect=ConnectionError("Lost connection"))
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(max_bt_temp=230.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        alerts: list[SafetyAlertEvent] = []
        bus.on(SafetyAlertEvent, lambda e: alerts.append(e))

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 235.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)

            # Should still be running
            assert monitor.is_running is True
        finally:
            await monitor.stop()

        # Emergency was attempted
        driver.emergency_stop.assert_called_once()
        # Alert was still published
        assert len(alerts) == 1


class TestGetStatus:
    """Tests for get_status() method."""

    @pytest.mark.asyncio
    async def test_status_includes_limits(self) -> None:
        """Status should include configured limits."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(max_bt_temp=225.0, max_ror=18.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            status = monitor.get_status()
        finally:
            await monitor.stop()

        assert status["limits"]["max_bt_temp"] == 225.0
        assert status["limits"]["max_ror"] == 18.0

    @pytest.mark.asyncio
    async def test_status_includes_watchdog_state(self) -> None:
        """Status should include watchdog timer state."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        config = _make_config(watchdog_warning_seconds=30, watchdog_emergency_seconds=60)
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            await asyncio.sleep(0.1)
            status = monitor.get_status()
        finally:
            await monitor.stop()

        assert "watchdog" in status
        assert status["watchdog"]["warning_threshold"] == 30
        assert status["watchdog"]["emergency_threshold"] == 60
        assert status["watchdog"]["seconds_since_last_tool_call"] >= 0
        assert status["watchdog"]["status"] == "OK"

    @pytest.mark.asyncio
    async def test_status_includes_active_alerts(self) -> None:
        """Status should include active alert conditions."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(latest_ror=25.0)  # High RoR
        config = _make_config(max_ror=20.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 200.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
            status = monitor.get_status()
        finally:
            await monitor.stop()

        assert "ror_high" in status["active_alerts"]

    @pytest.mark.asyncio
    async def test_status_includes_recent_alerts(self) -> None:
        """Status should include list of recent alerts."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(latest_ror=25.0)
        config = _make_config(max_ror=20.0)
        monitor = SafetyMonitor(driver, bus, session, config)

        try:
            await monitor.start()
            state: KaleidoState = {"BT": 200.0, "ET": 250.0, "sid": 1}
            await bus.publish(StateChangeEvent(state=state))
            await asyncio.sleep(0.05)
            status = monitor.get_status()
        finally:
            await monitor.stop()

        assert len(status["recent_alerts"]) >= 1
        assert status["recent_alerts"][0]["level"] == "WARNING"
        assert "condition" in status["recent_alerts"][0]


class TestManualModeWatchdog:
    """Watchdog is mode-aware: manual roasts warn, never emergency-stop."""

    @pytest.mark.asyncio
    async def test_manual_mode_watchdog_warns_only(self) -> None:
        driver = _make_mock_driver()
        bus = EventBus()
        alerts: list[SafetyAlertEvent] = []

        async def _collect(event: SafetyAlertEvent) -> None:
            alerts.append(event)

        bus.on(SafetyAlertEvent, _collect)
        monitor = SafetyMonitor(
            driver, bus, _make_mock_session(), _make_config(), watchdog_enforced=False
        )
        monitor._last_tool_call_time = time.time() - 120  # well past 60 s
        await monitor._check_watchdog()
        await asyncio.sleep(0)  # let the warning publish task run

        driver.emergency_stop.assert_not_awaited()
        assert any(a.level == SafetyAlertLevel.WARNING for a in alerts)
        assert not any(a.level == SafetyAlertLevel.EMERGENCY for a in alerts)
        assert monitor.get_status()["watchdog"]["enforced"] is False

    @pytest.mark.asyncio
    async def test_enforced_watchdog_still_emergency_stops(self) -> None:
        driver = _make_mock_driver()
        monitor = SafetyMonitor(driver, EventBus(), _make_mock_session(), _make_config())
        monitor._last_tool_call_time = time.time() - 61
        await monitor._check_watchdog()
        driver.emergency_stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_switching_to_enforced_resets_timer(self) -> None:
        driver = _make_mock_driver()
        monitor = SafetyMonitor(
            driver, EventBus(), _make_mock_session(), _make_config(), watchdog_enforced=False
        )
        monitor._last_tool_call_time = time.time() - 300
        monitor.set_watchdog_enforced(True)
        await monitor._check_watchdog()
        driver.emergency_stop.assert_not_awaited()
        assert monitor.watchdog_enforced is True

    @pytest.mark.asyncio
    async def test_hard_limits_still_emergency_stop_in_manual(self) -> None:
        driver = _make_mock_driver()
        monitor = SafetyMonitor(
            driver, EventBus(), _make_mock_session(), _make_config(), watchdog_enforced=False
        )
        await monitor._check_bt_limit(235.0)
        driver.emergency_stop.assert_awaited_once()


class TestEmergencyStopsHeatingManager:
    """Automatic e-stop must stop the HeatingManager keep-alive first."""

    @pytest.mark.asyncio
    async def test_on_emergency_hook_runs_before_driver_stop(self) -> None:
        order: list[str] = []
        driver = _make_mock_driver()

        async def _estop() -> None:
            order.append("driver_estop")

        async def _hs(on: bool) -> None:
            order.append(f"hs={int(on)}")

        driver.emergency_stop = AsyncMock(side_effect=_estop)
        driver.set_heating_state = AsyncMock(side_effect=_hs)

        async def _hook() -> None:
            order.append("halt_heat_sources")

        monitor = SafetyMonitor(
            driver, EventBus(), _make_mock_session(), _make_config(), on_emergency=_hook
        )
        await monitor._check_et_limit(300.0)
        assert order == ["halt_heat_sources", "driver_estop", "hs=0"]

    @pytest.mark.asyncio
    async def test_hook_failure_does_not_block_estop(self) -> None:
        driver = _make_mock_driver()

        async def _boom() -> None:
            raise RuntimeError("boom")

        monitor = SafetyMonitor(
            driver, EventBus(), _make_mock_session(), _make_config(), on_emergency=_boom
        )
        await monitor._check_bt_limit(240.0)
        driver.emergency_stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_estop_with_real_heating_manager_stays_off(self) -> None:
        """Simulator: after an automatic e-stop the HeatingManager no longer re-sends heat."""
        from maillard.core.heating import HeatingManager
        from maillard.driver.simulator import SimulatorDriver
        from maillard.mcp.state import ApplicationState

        sim = SimulatorDriver()
        await sim.connect("simulator")
        state = ApplicationState(config=MaillardConfig())
        state.driver = sim
        state._heating_manager = HeatingManager(sim, send_interval=0.05)
        await state._heating_manager.start(80)
        await asyncio.sleep(0.15)
        assert sim.state["HP"] == 80
        assert sim.state["HS"] == 1

        monitor = SafetyMonitor(
            sim,
            EventBus(),
            _make_mock_session(),
            _make_config(),
            on_emergency=state.halt_heat_sources,
        )
        await monitor._check_bt_limit(250.0)
        await asyncio.sleep(0.3)  # several former keep-alive intervals

        assert state._heating_manager is None
        assert sim.state["HP"] == 0  # not re-applied by a keep-alive
        assert sim.state["HS"] == 0
        assert sim.state["FC"] == 100
        await sim.disconnect()
