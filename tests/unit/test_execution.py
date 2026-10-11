"""Unit tests for RoastExecutionEngine.

Tests mode switching, phase interpolation, event detection, lifecycle management,
and manual override behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.core.events import (
    DropSuggested,
    DryEndSuggested,
    EventBus,
    FirstCrackSuggested,
    StateChangeEvent,
    TurningPointSuggested,
)
from maillard.db.schemas import PhaseSchema
from maillard.driver.base import KaleidoState
from maillard.roasting.execution import (
    ControlMode,
    RoastExecutionEngine,
    TargetSettings,
)
from maillard.roasting.session import RoastSession


def _make_mock_driver() -> MagicMock:
    """Create a mock driver for testing."""
    driver = MagicMock()
    driver.is_connected = True
    driver.set_heat = AsyncMock()
    driver.set_fan = AsyncMock()
    driver.set_drum = AsyncMock()
    driver.set_heating_state = AsyncMock()
    mock_state: KaleidoState = {"BT": 180.0, "ET": 200.0, "HP": 80, "FC": 40, "RC": 60, "sid": 1}
    driver.state = mock_state
    driver.read_state = AsyncMock(return_value=mock_state)
    return driver


def _make_mock_session(elapsed_time: float = 60.0) -> MagicMock:
    """Create a mock RoastSession for testing."""
    session = MagicMock(spec=RoastSession)
    session.elapsed_time = elapsed_time
    session.current_phase = MagicMock()
    session.current_phase.name = "drying"
    session.latest_ror = 8.5
    return session


def _make_profile_phases() -> list[PhaseSchema]:
    """Create a sample profile with multiple phases."""
    return [
        PhaseSchema(
            name="charge",
            target_time_seconds=0,
            target_temp=190.0,
            heat_percent=80,
            fan_percent=40,
            drum_percent=60,
        ),
        PhaseSchema(
            name="turning_point",
            target_time_seconds=60,
            target_temp=145.0,
            heat_percent=70,
            fan_percent=45,
            drum_percent=60,
        ),
        PhaseSchema(
            name="dry_end",
            target_time_seconds=240,
            target_temp=155.0,
            heat_percent=65,
            fan_percent=50,
            drum_percent=60,
        ),
        PhaseSchema(
            name="fc_start",
            target_time_seconds=420,
            target_temp=195.0,
            heat_percent=55,
            fan_percent=55,
            drum_percent=60,
        ),
        PhaseSchema(
            name="drop",
            target_time_seconds=600,
            target_temp=205.0,
            heat_percent=0,
            fan_percent=100,
            drum_percent=60,
        ),
    ]


class TestExecutionEngineLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_subscribes_to_events(self) -> None:
        """Engine should subscribe to StateChangeEvents on start."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        await engine.start()
        try:
            assert engine.is_running is True
            assert bus.handler_count >= 1
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(self) -> None:
        """Engine should unsubscribe from events on stop."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        await engine.start()
        await engine.stop()

        assert engine.is_running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start twice should not double-subscribe."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        try:
            await engine.start()
            handler_count_1 = bus.handler_count
            await engine.start()  # Should be no-op
            assert bus.handler_count == handler_count_1
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling stop multiple times should not raise."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        await engine.start()
        await engine.stop()
        await engine.stop()  # Should not raise

        assert engine.is_running is False


class TestModeSwitching:
    """Tests for control mode switching."""

    @pytest.mark.asyncio
    async def test_initial_mode_profile(self) -> None:
        """Engine should start in PROFILE mode by default."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        assert engine.mode == ControlMode.PROFILE

    @pytest.mark.asyncio
    async def test_initial_mode_manual(self) -> None:
        """Engine can start in MANUAL mode."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(
            driver, bus, session, phases, initial_mode=ControlMode.MANUAL
        )

        assert engine.mode == ControlMode.MANUAL

    @pytest.mark.asyncio
    async def test_switch_mode(self) -> None:
        """Engine should switch modes correctly."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        assert engine.mode == ControlMode.PROFILE

        engine.switch_mode(ControlMode.MANUAL)
        assert engine.mode == ControlMode.MANUAL

        engine.switch_mode(ControlMode.HOLD)
        assert engine.mode == ControlMode.HOLD

        engine.switch_mode(ControlMode.PROFILE)
        assert engine.mode == ControlMode.PROFILE

    @pytest.mark.asyncio
    async def test_manual_override_switches_mode(self) -> None:
        """Notifying manual override should switch from PROFILE to MANUAL."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        assert engine.mode == ControlMode.PROFILE

        engine.notify_manual_override("heat", 50)
        assert engine.mode == ControlMode.MANUAL

    @pytest.mark.asyncio
    async def test_manual_override_no_op_if_not_profile(self) -> None:
        """Manual override should not change mode if already in MANUAL or HOLD."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(
            driver, bus, session, phases, initial_mode=ControlMode.MANUAL
        )

        engine.notify_manual_override("heat", 50)
        assert engine.mode == ControlMode.MANUAL  # Still MANUAL

        engine.switch_mode(ControlMode.HOLD)
        engine.notify_manual_override("fan", 60)
        assert engine.mode == ControlMode.HOLD  # Still HOLD


class TestPhaseInterpolation:
    """Tests for profile phase interpolation."""

    def test_calculate_targets_at_charge(self) -> None:
        """At time 0, should use charge phase settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=0.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        targets = engine._calculate_targets(0.0)

        assert targets is not None
        assert targets.heat == 80
        assert targets.fan == 40
        assert targets.drum == 60
        assert targets.target_temp == 190.0

    def test_calculate_targets_at_phase_boundary(self) -> None:
        """At exact phase boundary, should use that phase's settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=60.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        targets = engine._calculate_targets(60.0)

        assert targets is not None
        # At 60s, we're at turning_point phase
        assert targets.heat == 70
        assert targets.fan == 45

    def test_calculate_targets_mid_phase(self) -> None:
        """Between phases, should interpolate settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=30.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        # At 30s, we're halfway between charge (0s) and turning_point (60s)
        targets = engine._calculate_targets(30.0)

        assert targets is not None
        # Interpolate heat: 80 -> 70, halfway = 75
        assert targets.heat == 75
        # Interpolate fan: 40 -> 45, halfway = 42 or 43 (rounding)
        assert 42 <= targets.fan <= 43
        # Drum stays constant
        assert targets.drum == 60

    def test_calculate_targets_past_last_phase(self) -> None:
        """Past last phase, should use last phase's settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=700.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        targets = engine._calculate_targets(700.0)

        assert targets is not None
        # Last phase (drop): heat=0, fan=100
        assert targets.heat == 0
        assert targets.fan == 100

    def test_calculate_targets_empty_phases(self) -> None:
        """With no phases, should return None."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        engine = RoastExecutionEngine(driver, bus, session, profile_phases=[])

        targets = engine._calculate_targets(60.0)
        assert targets is None


class TestEventDetection:
    """Tests for event suggestion detection."""

    @pytest.mark.asyncio
    async def test_turning_point_detection(self) -> None:
        """Engine should detect turning point when BT rises after minimum."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=0.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases, initial_mode=ControlMode.MANUAL)

        received: list[TurningPointSuggested] = []
        bus.on(TurningPointSuggested, lambda e: received.append(e))

        await engine.start()
        try:
            # Simulate BT dropping then rising (typical turning point pattern)
            # First, track decreasing BT
            for i, bt in enumerate([180, 170, 160, 150, 145]):
                session.elapsed_time = float(i * 10)
                engine._track_bt(session.elapsed_time, float(bt))

            # Now track rising BT (past turning point)
            for i, bt in enumerate([146, 147, 148, 150, 152]):
                session.elapsed_time = float(50 + i * 10)
                engine._track_bt(session.elapsed_time, float(bt))
                await engine._check_event_suggestions(float(bt), session.elapsed_time)
        finally:
            await engine.stop()

        # Should have detected turning point
        assert len(received) >= 1
        assert received[0].min_bt_detected == 145

    @pytest.mark.asyncio
    async def test_dry_end_suggestion(self) -> None:
        """Engine should suggest dry end when approaching threshold."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=200.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(
            driver, bus, session, phases,
            initial_mode=ControlMode.MANUAL,
            dry_end_threshold=150.0,
        )

        received: list[DryEndSuggested] = []
        bus.on(DryEndSuggested, lambda e: received.append(e))

        await engine.start()
        try:
            # BT approaching dry end threshold (150 - 5 = 145)
            await engine._check_event_suggestions(146.0, 200.0)
        finally:
            await engine.stop()

        assert len(received) == 1
        assert received[0].threshold_temp == 150.0

    @pytest.mark.asyncio
    async def test_fc_suggestion(self) -> None:
        """Engine should suggest first crack when approaching threshold."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=400.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(
            driver, bus, session, phases,
            initial_mode=ControlMode.MANUAL,
            fc_threshold=195.0,
        )

        received: list[FirstCrackSuggested] = []
        bus.on(FirstCrackSuggested, lambda e: received.append(e))

        await engine.start()
        try:
            # BT approaching FC threshold (195 - 5 = 190)
            await engine._check_event_suggestions(191.0, 400.0)
        finally:
            await engine.stop()

        assert len(received) == 1
        assert received[0].threshold_temp == 195.0

    @pytest.mark.asyncio
    async def test_drop_suggestion(self) -> None:
        """Engine should suggest drop when profile target reached."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=600.0)
        phases = _make_profile_phases()  # Last phase: drop at 600s, 205°C
        engine = RoastExecutionEngine(driver, bus, session, phases, initial_mode=ControlMode.MANUAL)

        received: list[DropSuggested] = []
        bus.on(DropSuggested, lambda e: received.append(e))

        await engine.start()
        try:
            # At target time
            await engine._check_event_suggestions(203.0, 600.0)
        finally:
            await engine.stop()

        assert len(received) == 1
        assert received[0].target_time == 600.0
        assert received[0].target_temp == 205.0

    @pytest.mark.asyncio
    async def test_no_duplicate_suggestions(self) -> None:
        """Engine should not suggest the same event twice."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=400.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(
            driver, bus, session, phases,
            initial_mode=ControlMode.MANUAL,
            fc_threshold=195.0,
        )

        received: list[FirstCrackSuggested] = []
        bus.on(FirstCrackSuggested, lambda e: received.append(e))

        await engine.start()
        try:
            # Trigger FC suggestion multiple times
            await engine._check_event_suggestions(191.0, 400.0)
            await engine._check_event_suggestions(192.0, 410.0)
            await engine._check_event_suggestions(193.0, 420.0)
        finally:
            await engine.stop()

        # Should only receive one suggestion
        assert len(received) == 1


class TestProfileFollowing:
    """Tests for automatic profile following in PROFILE mode."""

    @pytest.mark.asyncio
    async def test_applies_controls_in_profile_mode(self) -> None:
        """In PROFILE mode, engine should apply control settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=30.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        await engine.start()
        try:
            # Trigger state change to apply controls
            await engine._apply_profile_controls(30.0)
        finally:
            await engine.stop()

        # Should have called set_heat, set_fan, set_drum
        assert driver.set_heat.called
        assert driver.set_fan.called
        assert driver.set_drum.called

    @pytest.mark.asyncio
    async def test_no_controls_in_manual_mode(self) -> None:
        """In MANUAL mode, engine should not apply control settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=30.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(
            driver, bus, session, phases, initial_mode=ControlMode.MANUAL
        )

        await engine.start()
        try:
            # Simulate state change event
            state: KaleidoState = {"BT": 160.0, "ET": 180.0, "HP": 70, "FC": 45, "RC": 60, "sid": 1}
            event = StateChangeEvent(state=state)
            await engine._on_state_change(event)
        finally:
            await engine.stop()

        # Should NOT have called control setters
        assert not driver.set_heat.called
        assert not driver.set_fan.called
        assert not driver.set_drum.called


class TestGetStatus:
    """Tests for get_status method."""

    def test_status_includes_mode(self) -> None:
        """Status should include current mode."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        status = engine.get_status()

        assert status["mode"] == "profile"

    def test_status_includes_phase_info(self) -> None:
        """Status should include current phase information."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=30.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        status = engine.get_status()

        assert "current_phase" in status
        assert status["current_phase"]["name"] == "charge"
        assert "next_phase" in status
        assert status["next_phase"]["name"] == "turning_point"

    def test_status_includes_targets(self) -> None:
        """Status should include target settings."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session(elapsed_time=30.0)
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        status = engine.get_status()

        assert "targets" in status
        assert "heat_percent" in status["targets"]
        assert "fan_percent" in status["targets"]
        assert "drum_percent" in status["targets"]

    def test_status_includes_suggested_events(self) -> None:
        """Status should include list of suggested events."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        # Mark some events as suggested
        engine._suggested_events.add("turning_point")
        engine._suggested_events.add("dry_end")

        status = engine.get_status()

        assert "suggested_events" in status
        assert "turning_point" in status["suggested_events"]
        assert "dry_end" in status["suggested_events"]


class TestMarkEventConfirmed:
    """Tests for mark_event_confirmed method."""

    def test_mark_event_prevents_re_suggestion(self) -> None:
        """Marking an event as confirmed should prevent re-suggestion."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        phases = _make_profile_phases()
        engine = RoastExecutionEngine(driver, bus, session, phases)

        # Mark event
        engine.mark_event_confirmed("turning_point")

        assert "turning_point" in engine._suggested_events
