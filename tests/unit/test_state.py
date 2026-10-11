"""Unit tests for roast state machine.

Tests cover:
- State transitions through normal roast workflow
- Sub-phase transitions within ROASTING state
- Abort transitions from all non-final states
- Reset transitions from final states
- Invalid transition rejection
- Roast data tracking
- Callbacks for state and phase changes
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from statemachine.exceptions import TransitionNotAllowed

from maillard.roasting.state import (
    DEFAULT_DRY_END_TEMP,
    DEFAULT_FC_TEMP,
    InvalidStateError,
    RoastPhase,
    RoastState,
    RoastStateMachine,
)


class TestRoastStateMachineInitialization:
    """Tests for state machine initialization."""

    def test_initial_state_is_idle(self) -> None:
        """State machine should start in IDLE state."""
        machine = RoastStateMachine()
        assert machine.state_enum == RoastState.IDLE
        assert machine.current_state == machine.idle

    def test_initial_phase_is_none(self) -> None:
        """Sub-phase should be None when not roasting."""
        machine = RoastStateMachine()
        assert machine.current_phase is None

    def test_custom_temperature_thresholds(self) -> None:
        """Should accept custom temperature thresholds."""
        machine = RoastStateMachine(dry_end_temp=160.0, fc_temp=200.0)
        assert machine._dry_end_temp == 160.0
        assert machine._fc_temp == 200.0

    def test_default_temperature_thresholds(self) -> None:
        """Should use default temperature thresholds."""
        machine = RoastStateMachine()
        assert machine._dry_end_temp == DEFAULT_DRY_END_TEMP
        assert machine._fc_temp == DEFAULT_FC_TEMP

    def test_no_roast_data_initially(self) -> None:
        """All roast data should be None initially."""
        machine = RoastStateMachine()
        assert machine.charge_time is None
        assert machine.charge_temp is None
        assert machine.turning_point_time is None
        assert machine.dry_end_time is None
        assert machine.fc_start_time is None
        assert machine.drop_time is None


class TestNormalWorkflowTransitions:
    """Tests for normal roast workflow transitions."""

    def test_idle_to_preheating(self) -> None:
        """Should transition from IDLE to PREHEATING."""
        machine = RoastStateMachine()
        machine.start_preheat(target_temp=200.0)
        assert machine.state_enum == RoastState.PREHEATING
        assert machine.target_charge_temp == 200.0

    def test_preheating_to_ready(self) -> None:
        """Should transition from PREHEATING to READY_TO_CHARGE."""
        machine = RoastStateMachine()
        machine.start_preheat(target_temp=200.0)
        machine.mark_ready()
        assert machine.state_enum == RoastState.READY_TO_CHARGE

    def test_ready_to_roasting(self) -> None:
        """Should transition from READY_TO_CHARGE to ROASTING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge(temp=195.0)
        assert machine.state_enum == RoastState.ROASTING
        assert machine.charge_temp == 195.0
        assert machine.charge_time is not None

    def test_roasting_to_cooling(self) -> None:
        """Should transition from ROASTING to COOLING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge(temp=195.0)
        machine.drop(temp=210.0, time_seconds=600)
        assert machine.state_enum == RoastState.COOLING
        assert machine.drop_temp == 210.0
        assert machine.drop_time == 600

    def test_cooling_to_completed(self) -> None:
        """Should transition from COOLING to COMPLETED."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.drop()
        machine.complete()
        assert machine.state_enum == RoastState.COMPLETED

    def test_full_roast_workflow(self) -> None:
        """Should complete full roast workflow."""
        machine = RoastStateMachine()

        # Preheat
        machine.start_preheat(target_temp=200.0)
        assert machine.state_enum == RoastState.PREHEATING

        # Ready
        machine.mark_ready()
        assert machine.state_enum == RoastState.READY_TO_CHARGE

        # Charge
        machine.charge(temp=195.0)
        assert machine.state_enum == RoastState.ROASTING
        assert machine.current_phase == RoastPhase.DRYING

        # Roast events
        machine.mark_turning_point(temp=95.0, time_seconds=60)
        machine.mark_dry_end(temp=150.0, time_seconds=240)
        assert machine.current_phase == RoastPhase.MAILLARD

        machine.mark_first_crack(temp=196.0, time_seconds=450)
        assert machine.current_phase == RoastPhase.DEVELOPMENT

        # Drop
        machine.drop(temp=210.0, time_seconds=600)
        assert machine.state_enum == RoastState.COOLING
        assert machine.current_phase is None

        # Complete
        machine.complete()
        assert machine.state_enum == RoastState.COMPLETED


class TestSkipPreheatTransition:
    """Tests for skip_to_ready transition."""

    def test_skip_preheat(self) -> None:
        """Should allow skipping preheat directly to READY_TO_CHARGE."""
        machine = RoastStateMachine()
        machine.skip_to_ready()
        assert machine.state_enum == RoastState.READY_TO_CHARGE

    def test_skip_preheat_then_charge(self) -> None:
        """Should be able to charge after skipping preheat."""
        machine = RoastStateMachine()
        machine.skip_to_ready()
        machine.charge(temp=190.0)
        assert machine.state_enum == RoastState.ROASTING


class TestSubPhaseTransitions:
    """Tests for sub-phase transitions within ROASTING state."""

    @pytest.fixture
    def roasting_machine(self) -> RoastStateMachine:
        """Create a machine in ROASTING state."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge(temp=195.0)
        return machine

    def test_charge_starts_drying_phase(self, roasting_machine: RoastStateMachine) -> None:
        """Charging should start in DRYING phase."""
        assert roasting_machine.current_phase == RoastPhase.DRYING

    def test_dry_end_transitions_to_maillard(self, roasting_machine: RoastStateMachine) -> None:
        """Marking dry end should transition to MAILLARD phase."""
        roasting_machine.mark_dry_end(temp=150.0, time_seconds=240)
        assert roasting_machine.current_phase == RoastPhase.MAILLARD
        assert roasting_machine.dry_end_temp == 150.0
        assert roasting_machine.dry_end_time == 240

    def test_first_crack_transitions_to_development(
        self, roasting_machine: RoastStateMachine
    ) -> None:
        """Marking first crack should transition to DEVELOPMENT phase."""
        roasting_machine.mark_dry_end(temp=150.0, time_seconds=240)
        roasting_machine.mark_first_crack(temp=196.0, time_seconds=450)
        assert roasting_machine.current_phase == RoastPhase.DEVELOPMENT
        assert roasting_machine.fc_start_temp == 196.0
        assert roasting_machine.fc_start_time == 450

    def test_turning_point_tracked(self, roasting_machine: RoastStateMachine) -> None:
        """Turning point should be tracked without changing phase."""
        roasting_machine.mark_turning_point(temp=95.0, time_seconds=60)
        assert roasting_machine.turning_point_temp == 95.0
        assert roasting_machine.turning_point_time == 60
        assert roasting_machine.current_phase == RoastPhase.DRYING  # Phase unchanged

    def test_first_crack_end_tracked(self, roasting_machine: RoastStateMachine) -> None:
        """First crack end should be tracked without changing phase."""
        roasting_machine.mark_dry_end(temp=150.0, time_seconds=240)
        roasting_machine.mark_first_crack(temp=196.0, time_seconds=450)
        roasting_machine.mark_first_crack_end(temp=200.0, time_seconds=480)
        assert roasting_machine.fc_end_temp == 200.0
        assert roasting_machine.fc_end_time == 480
        assert roasting_machine.current_phase == RoastPhase.DEVELOPMENT  # Phase unchanged

    def test_second_crack_tracked(self, roasting_machine: RoastStateMachine) -> None:
        """Second crack should be tracked."""
        roasting_machine.mark_dry_end(temp=150.0, time_seconds=240)
        roasting_machine.mark_first_crack(temp=196.0, time_seconds=450)
        roasting_machine.mark_second_crack(temp=224.0, time_seconds=540)
        assert roasting_machine.sc_start_temp == 224.0
        assert roasting_machine.sc_start_time == 540

    def test_drop_clears_phase(self, roasting_machine: RoastStateMachine) -> None:
        """Dropping should clear the current phase."""
        roasting_machine.mark_dry_end(temp=150.0, time_seconds=240)
        roasting_machine.mark_first_crack(temp=196.0, time_seconds=450)
        roasting_machine.drop(temp=210.0, time_seconds=600)
        assert roasting_machine.current_phase is None


class TestAbortTransitions:
    """Tests for abort transitions from all non-final states."""

    def test_abort_from_idle(self) -> None:
        """Should be able to abort from IDLE."""
        machine = RoastStateMachine()
        machine.abort()
        assert machine.state_enum == RoastState.ABORTED

    def test_abort_from_preheating(self) -> None:
        """Should be able to abort from PREHEATING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.abort()
        assert machine.state_enum == RoastState.ABORTED

    def test_abort_from_ready_to_charge(self) -> None:
        """Should be able to abort from READY_TO_CHARGE."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.abort()
        assert machine.state_enum == RoastState.ABORTED

    def test_abort_from_roasting(self) -> None:
        """Should be able to abort from ROASTING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.abort(reason="Emergency")
        assert machine.state_enum == RoastState.ABORTED
        assert machine.current_phase is None  # Phase cleared

    def test_abort_from_cooling(self) -> None:
        """Should be able to abort from COOLING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.drop()
        machine.abort()
        assert machine.state_enum == RoastState.ABORTED


class TestResetTransitions:
    """Tests for reset transitions from final states."""

    def test_reset_from_completed(self) -> None:
        """Should be able to reset from COMPLETED."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge(temp=195.0)
        machine.drop(temp=210.0, time_seconds=600)
        machine.complete()
        assert machine.state_enum == RoastState.COMPLETED

        machine.reset()
        assert machine.state_enum == RoastState.IDLE
        # Data should be cleared
        assert machine.charge_temp is None
        assert machine.drop_temp is None

    def test_reset_from_aborted(self) -> None:
        """Should be able to reset from ABORTED."""
        machine = RoastStateMachine()
        machine.abort()
        machine.reset()
        assert machine.state_enum == RoastState.IDLE

    def test_reset_from_cooling(self) -> None:
        """Should be able to reset from COOLING (cancel without completing)."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.drop()
        machine.reset()
        assert machine.state_enum == RoastState.IDLE


class TestInvalidTransitions:
    """Tests for invalid transition rejection."""

    def test_cannot_charge_from_idle(self) -> None:
        """Should not be able to charge directly from IDLE."""
        machine = RoastStateMachine()
        with pytest.raises(TransitionNotAllowed):
            machine.charge()

    def test_cannot_drop_from_idle(self) -> None:
        """Should not be able to drop from IDLE."""
        machine = RoastStateMachine()
        with pytest.raises(TransitionNotAllowed):
            machine.drop()

    def test_cannot_charge_from_preheating(self) -> None:
        """Should not be able to charge from PREHEATING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        with pytest.raises(TransitionNotAllowed):
            machine.charge()

    def test_cannot_complete_from_roasting(self) -> None:
        """Should not be able to complete from ROASTING."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        with pytest.raises(TransitionNotAllowed):
            machine.complete()

    def test_cannot_abort_from_completed(self) -> None:
        """Should not be able to abort from COMPLETED (already in final state)."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.drop()
        machine.complete()
        assert machine.is_final
        # Abort is not defined as a transition from completed
        with pytest.raises(TransitionNotAllowed):
            machine.abort()

    def test_cannot_abort_from_aborted(self) -> None:
        """Should not be able to abort from ABORTED (already in final state)."""
        machine = RoastStateMachine()
        machine.abort()
        assert machine.is_final
        # Abort is not defined as a transition from aborted
        with pytest.raises(TransitionNotAllowed):
            machine.abort()

    def test_cannot_mark_events_outside_roasting(self) -> None:
        """Should not be able to mark roast events outside ROASTING state."""
        machine = RoastStateMachine()

        with pytest.raises(InvalidStateError):
            machine.mark_turning_point(temp=95.0, time_seconds=60)

        with pytest.raises(InvalidStateError):
            machine.mark_dry_end(temp=150.0, time_seconds=240)

        with pytest.raises(InvalidStateError):
            machine.mark_first_crack(temp=196.0, time_seconds=450)


class TestCallbacks:
    """Tests for state and phase change callbacks."""

    def test_state_change_callback_called(self) -> None:
        """Should call state change callback on transitions."""
        callback = MagicMock()
        machine = RoastStateMachine(on_state_change=callback)

        machine.start_preheat()
        callback.assert_called_once_with(RoastState.IDLE, RoastState.PREHEATING)

        callback.reset_mock()
        machine.mark_ready()
        callback.assert_called_once_with(RoastState.PREHEATING, RoastState.READY_TO_CHARGE)

    def test_phase_change_callback_called(self) -> None:
        """Should call phase change callback on phase transitions."""
        callback = MagicMock()
        machine = RoastStateMachine(on_phase_change=callback)
        machine.start_preheat()
        machine.mark_ready()

        machine.charge()
        callback.assert_called_once_with(None, RoastPhase.DRYING)

        callback.reset_mock()
        machine.mark_dry_end(temp=150.0, time_seconds=240)
        callback.assert_called_once_with(RoastPhase.DRYING, RoastPhase.MAILLARD)

        callback.reset_mock()
        machine.mark_first_crack(temp=196.0, time_seconds=450)
        callback.assert_called_once_with(RoastPhase.MAILLARD, RoastPhase.DEVELOPMENT)

        callback.reset_mock()
        machine.drop()
        callback.assert_called_once_with(RoastPhase.DEVELOPMENT, None)


class TestPropertyMethods:
    """Tests for property methods."""

    def test_is_roasting_property(self) -> None:
        """is_roasting should return True only in ROASTING state."""
        machine = RoastStateMachine()
        assert not machine.is_roasting

        machine.start_preheat()
        assert not machine.is_roasting

        machine.mark_ready()
        assert not machine.is_roasting

        machine.charge()
        assert machine.is_roasting

        machine.drop()
        assert not machine.is_roasting

    def test_is_active_property(self) -> None:
        """is_active should return True when roast session is active."""
        machine = RoastStateMachine()
        assert not machine.is_active  # IDLE

        machine.start_preheat()
        assert machine.is_active  # PREHEATING

        machine.mark_ready()
        assert machine.is_active  # READY_TO_CHARGE

        machine.charge()
        assert machine.is_active  # ROASTING

        machine.drop()
        assert machine.is_active  # COOLING

        machine.complete()
        assert not machine.is_active  # COMPLETED (final)

    def test_is_final_property(self) -> None:
        """is_final should return True only in COMPLETED or ABORTED states."""
        machine = RoastStateMachine()
        assert not machine.is_final  # IDLE

        machine.start_preheat()
        assert not machine.is_final  # PREHEATING

        machine.mark_ready()
        machine.charge()
        assert not machine.is_final  # ROASTING

        machine.drop()
        machine.complete()
        assert machine.is_final  # COMPLETED

        machine.reset()
        machine.abort()
        assert machine.is_final  # ABORTED

    def test_elapsed_time_property(self) -> None:
        """elapsed_time should return time since charge."""
        machine = RoastStateMachine()
        assert machine.elapsed_time is None  # Not charged yet

        # Mock datetime to control time
        fixed_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        with patch("maillard.roasting.state.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time

            machine.start_preheat()
            machine.mark_ready()
            machine.charge()

            # Now return a time 300 seconds later
            mock_dt.now.return_value = fixed_time + timedelta(seconds=300)

            # elapsed_time calls datetime.now(UTC)
            elapsed = machine.elapsed_time
            assert elapsed is not None
            # Should be approximately 300 seconds (may have small delta due to implementation)
            assert abs(elapsed - 300) < 1

    def test_state_enum_property(self) -> None:
        """state_enum should return correct RoastState enum."""
        machine = RoastStateMachine()
        assert machine.state_enum == RoastState.IDLE

        machine.start_preheat()
        assert machine.state_enum == RoastState.PREHEATING

        machine.mark_ready()
        assert machine.state_enum == RoastState.READY_TO_CHARGE


class TestGetRoastSummary:
    """Tests for get_roast_summary method."""

    def test_summary_includes_all_data(self) -> None:
        """Summary should include all roast data."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge(temp=195.0)
        machine.mark_turning_point(temp=95.0, time_seconds=60)
        machine.mark_dry_end(temp=150.0, time_seconds=240)
        machine.mark_first_crack(temp=196.0, time_seconds=450)
        machine.mark_first_crack_end(temp=200.0, time_seconds=480)
        machine.drop(temp=210.0, time_seconds=600)

        summary = machine.get_roast_summary()

        assert summary["state"] == "cooling"
        assert summary["phase"] is None  # Phase cleared after drop
        assert summary["charge_temp"] == 195.0
        assert summary["turning_point_temp"] == 95.0
        assert summary["turning_point_time"] == 60
        assert summary["dry_end_temp"] == 150.0
        assert summary["dry_end_time"] == 240
        assert summary["fc_start_temp"] == 196.0
        assert summary["fc_start_time"] == 450
        assert summary["fc_end_temp"] == 200.0
        assert summary["fc_end_time"] == 480
        assert summary["drop_temp"] == 210.0
        assert summary["drop_time"] == 600
        assert summary["charge_time"] is not None

    def test_summary_with_minimal_data(self) -> None:
        """Summary should work with minimal roast data."""
        machine = RoastStateMachine()
        summary = machine.get_roast_summary()

        assert summary["state"] == "idle"
        assert summary["phase"] is None
        assert summary["charge_temp"] is None
        assert summary["drop_temp"] is None


class TestDropTimeCalculation:
    """Tests for automatic drop time calculation."""

    def test_drop_time_auto_calculated(self) -> None:
        """Drop time should be auto-calculated if not provided."""
        machine = RoastStateMachine()

        # Set up a known charge time
        fixed_charge_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        fixed_drop_time = fixed_charge_time + timedelta(seconds=500)

        with patch("maillard.roasting.state.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_charge_time

            machine.start_preheat()
            machine.mark_ready()
            machine.charge()

            # Move time forward for drop
            mock_dt.now.return_value = fixed_drop_time
            machine.drop(temp=210.0)

            # drop_time should be calculated as ~500 seconds
            assert machine.drop_time is not None
            assert abs(machine.drop_time - 500) < 1

    def test_drop_time_explicit(self) -> None:
        """Explicit drop time should be used when provided."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.drop(temp=210.0, time_seconds=600)

        assert machine.drop_time == 600


class TestRepr:
    """Tests for string representation."""

    def test_repr_idle(self) -> None:
        """repr should show state when idle."""
        machine = RoastStateMachine()
        assert "state=idle" in repr(machine)
        assert "phase=" not in repr(machine)

    def test_repr_roasting_with_phase(self) -> None:
        """repr should show state and phase when roasting."""
        machine = RoastStateMachine()
        machine.start_preheat()
        machine.mark_ready()
        machine.charge()
        machine.mark_dry_end(temp=150.0, time_seconds=240)

        r = repr(machine)
        assert "state=roasting" in r
        assert "phase=MAILLARD" in r
