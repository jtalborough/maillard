"""Roast state machine for managing roast workflow.

This module implements a state machine for tracking roast progress using
the python-statemachine library. It manages transitions through roast phases
and sub-phases within the ROASTING state.

State Diagram:
    IDLE → PREHEATING → READY_TO_CHARGE → ROASTING → COOLING → COMPLETED
       ↓          ↓              ↓             ↓          ↓
       └──────────┴──────────────┴─────────────┴──────────┴─→ ABORTED

Sub-phases within ROASTING:
    drying → maillard → development
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Final

from statemachine import State, StateMachine

if TYPE_CHECKING:
    from collections.abc import Callable

_logger = logging.getLogger(__name__)


class InvalidStateError(Exception):
    """Raised when an operation is attempted in an invalid state.

    This exception is raised when trying to mark roast events (turning point,
    dry end, first crack, etc.) while not in the ROASTING state.
    """


class RoastPhase(Enum):
    """Sub-phases within the ROASTING state.

    These phases track progress through the roast once beans are charged.
    Transitions are based on temperature milestones:
    - DRYING: From charge until ~150°C (moisture evaporation)
    - MAILLARD: From dry end until first crack (~150-196°C)
    - DEVELOPMENT: From first crack until drop
    """

    DRYING = auto()
    MAILLARD = auto()
    DEVELOPMENT = auto()


class RoastState(Enum):
    """Main roast workflow states.

    These represent the high-level stages of a roast session.
    """

    IDLE = "idle"  # No active roast, ready to start
    PREHEATING = "preheating"  # Heating up to charge temperature
    READY_TO_CHARGE = "ready_to_charge"  # At charge temp, waiting for beans
    ROASTING = "roasting"  # Active roast in progress
    COOLING = "cooling"  # Beans dropped, cooling phase
    COMPLETED = "completed"  # Roast finished successfully
    ABORTED = "aborted"  # Roast terminated abnormally


# Default temperature thresholds for phase transitions (Celsius)
DEFAULT_DRY_END_TEMP: Final[float] = 150.0  # End of drying phase
DEFAULT_FC_TEMP: Final[float] = 196.0  # First crack temperature
DEFAULT_CHARGE_TEMP_TOLERANCE: Final[float] = 5.0  # °C from target to be "ready"


class RoastStateMachine(StateMachine):
    """State machine for managing roast workflow.

    This state machine tracks the high-level roast states and enforces
    valid transitions between them. It also tracks sub-phases within
    the ROASTING state (drying, maillard, development).

    Usage:
        machine = RoastStateMachine()

        # Start preheating
        machine.start_preheat()  # IDLE → PREHEATING

        # Mark ready to charge when at temperature
        machine.mark_ready()  # PREHEATING → READY_TO_CHARGE

        # Charge beans
        machine.charge()  # READY_TO_CHARGE → ROASTING

        # Mark roast events (these track sub-phases)
        machine.mark_turning_point(temp=95.0, time_seconds=60)
        machine.mark_dry_end(temp=150.0, time_seconds=240)
        machine.mark_first_crack(temp=196.0, time_seconds=450)

        # Drop beans
        machine.drop()  # ROASTING → COOLING

        # Complete the roast
        machine.complete()  # COOLING → COMPLETED

    Abort from any state:
        machine.abort()  # Any → ABORTED

    Attributes:
        current_phase: Current sub-phase within ROASTING (if applicable)
        charge_time: Timestamp when beans were charged
        charge_temp: Bean temperature at charge
        turning_point_time: Seconds from charge to turning point
        turning_point_temp: Bean temperature at turning point
        dry_end_time: Seconds from charge to dry end
        dry_end_temp: Bean temperature at dry end
        fc_start_time: Seconds from charge to first crack
        fc_start_temp: Bean temperature at first crack
        fc_end_time: Seconds from charge to FC end
        fc_end_temp: Bean temperature at FC end
        sc_start_time: Seconds from charge to second crack
        sc_start_temp: Bean temperature at second crack
        drop_time: Seconds from charge to drop
        drop_temp: Bean temperature at drop
    """

    # Define states
    # Note: completed and aborted are not marked as final=True to allow
    # reset transitions back to idle for starting new roasts.
    idle = State(initial=True)
    preheating = State()
    ready_to_charge = State()
    roasting = State()
    cooling = State()
    completed = State()
    aborted = State()

    # Define transitions
    # Normal workflow
    start_preheat = idle.to(preheating)
    mark_ready = preheating.to(ready_to_charge)
    charge = ready_to_charge.to(roasting)
    drop = roasting.to(cooling)
    complete = cooling.to(completed)

    # Skip preheat (for resuming or testing)
    skip_to_ready = idle.to(ready_to_charge)

    # Reset transitions (for starting a new roast)
    reset = completed.to(idle) | aborted.to(idle) | cooling.to(idle)

    # Abort transition (from any non-final state)
    abort = (
        idle.to(aborted)
        | preheating.to(aborted)
        | ready_to_charge.to(aborted)
        | roasting.to(aborted)
        | cooling.to(aborted)
    )

    def __init__(
        self,
        dry_end_temp: float = DEFAULT_DRY_END_TEMP,
        fc_temp: float = DEFAULT_FC_TEMP,
        on_state_change: Callable[[RoastState, RoastState], None] | None = None,
        on_phase_change: Callable[[RoastPhase | None, RoastPhase | None], None] | None = None,
    ) -> None:
        """Initialize the roast state machine.

        Args:
            dry_end_temp: Temperature threshold for drying → maillard transition
            fc_temp: Temperature threshold for maillard → development transition
            on_state_change: Callback when main state changes (old_state, new_state)
            on_phase_change: Callback when sub-phase changes (old_phase, new_phase)
        """
        # Initialize all instance attributes BEFORE calling super().__init__()
        # because python-statemachine may trigger callbacks during initialization.

        # Configuration
        self._dry_end_temp = dry_end_temp
        self._fc_temp = fc_temp
        self._on_state_change = on_state_change
        self._on_phase_change = on_phase_change

        # Sub-phase tracking
        self._current_phase: RoastPhase | None = None

        # Roast data
        self._charge_time: datetime | None = None
        self._charge_temp: float | None = None
        self._turning_point_time: float | None = None
        self._turning_point_temp: float | None = None
        self._dry_end_time: float | None = None
        self._dry_end_temp_actual: float | None = None
        self._fc_start_time: float | None = None
        self._fc_start_temp: float | None = None
        self._fc_end_time: float | None = None
        self._fc_end_temp: float | None = None
        self._sc_start_time: float | None = None
        self._sc_start_temp: float | None = None
        self._drop_time: float | None = None
        self._drop_temp: float | None = None

        # Target temperature for preheat
        self._target_charge_temp: float | None = None

        # Now call parent init which may trigger state entry callbacks
        super().__init__()

    # Properties for read-only access to roast data
    @property
    def current_phase(self) -> RoastPhase | None:
        """Current sub-phase within ROASTING state."""
        return self._current_phase

    @property
    def state_enum(self) -> RoastState:
        """Current state as RoastState enum."""
        return RoastState(self.current_state.id)

    @property
    def charge_time(self) -> datetime | None:
        """Timestamp when beans were charged."""
        return self._charge_time

    @property
    def charge_temp(self) -> float | None:
        """Bean temperature at charge."""
        return self._charge_temp

    @property
    def turning_point_time(self) -> float | None:
        """Seconds from charge to turning point."""
        return self._turning_point_time

    @property
    def turning_point_temp(self) -> float | None:
        """Bean temperature at turning point."""
        return self._turning_point_temp

    @property
    def dry_end_time(self) -> float | None:
        """Seconds from charge to dry end."""
        return self._dry_end_time

    @property
    def dry_end_temp(self) -> float | None:
        """Bean temperature at dry end."""
        return self._dry_end_temp_actual

    @property
    def fc_start_time(self) -> float | None:
        """Seconds from charge to first crack start."""
        return self._fc_start_time

    @property
    def fc_start_temp(self) -> float | None:
        """Bean temperature at first crack start."""
        return self._fc_start_temp

    @property
    def fc_end_time(self) -> float | None:
        """Seconds from charge to first crack end."""
        return self._fc_end_time

    @property
    def fc_end_temp(self) -> float | None:
        """Bean temperature at first crack end."""
        return self._fc_end_temp

    @property
    def sc_start_time(self) -> float | None:
        """Seconds from charge to second crack start."""
        return self._sc_start_time

    @property
    def sc_start_temp(self) -> float | None:
        """Bean temperature at second crack start."""
        return self._sc_start_temp

    @property
    def drop_time(self) -> float | None:
        """Seconds from charge to drop."""
        return self._drop_time

    @property
    def drop_temp(self) -> float | None:
        """Bean temperature at drop."""
        return self._drop_temp

    @property
    def target_charge_temp(self) -> float | None:
        """Target temperature for preheating."""
        return self._target_charge_temp

    @property
    def is_roasting(self) -> bool:
        """Check if actively roasting (in ROASTING state)."""
        return bool(self.current_state == self.roasting)

    @property
    def is_active(self) -> bool:
        """Check if roast session is active (not idle, completed, or aborted)."""
        return self.current_state not in (self.idle, self.completed, self.aborted)

    @property
    def is_final(self) -> bool:
        """Check if roast is in a final state (completed or aborted).

        Final states indicate the roast session has ended and can only
        transition back to idle via reset.
        """
        return self.current_state in (self.completed, self.aborted)

    @property
    def elapsed_time(self) -> float | None:
        """Time elapsed since charge in seconds, or None if not charged."""
        if self._charge_time is None:
            return None
        return (datetime.now(UTC) - self._charge_time).total_seconds()

    def _set_phase(self, new_phase: RoastPhase | None) -> None:
        """Update the current sub-phase and trigger callback."""
        if new_phase != self._current_phase:
            old_phase = self._current_phase
            self._current_phase = new_phase
            _logger.debug("Phase transition: %s → %s", old_phase, new_phase)
            if self._on_phase_change:
                self._on_phase_change(old_phase, new_phase)

    def _clear_roast_data(self) -> None:
        """Clear all roast tracking data."""
        self._current_phase = None
        self._charge_time = None
        self._charge_temp = None
        self._turning_point_time = None
        self._turning_point_temp = None
        self._dry_end_time = None
        self._dry_end_temp_actual = None
        self._fc_start_time = None
        self._fc_start_temp = None
        self._fc_end_time = None
        self._fc_end_temp = None
        self._sc_start_time = None
        self._sc_start_temp = None
        self._drop_time = None
        self._drop_temp = None
        self._target_charge_temp = None

    # Transition callbacks

    def on_start_preheat(self, target_temp: float | None = None) -> None:
        """Called when starting preheat.

        Args:
            target_temp: Target charge temperature in °C
        """
        self._target_charge_temp = target_temp
        _logger.info("Starting preheat, target: %s°C", target_temp)

    def on_charge(self, temp: float | None = None) -> None:
        """Called when beans are charged.

        Args:
            temp: Bean temperature at charge
        """
        self._charge_time = datetime.now(UTC)
        self._charge_temp = temp
        self._set_phase(RoastPhase.DRYING)
        _logger.info("CHARGE at %.1f°C", temp or 0)

    def on_drop(self, temp: float | None = None, time_seconds: float | None = None) -> None:
        """Called when beans are dropped.

        Args:
            temp: Bean temperature at drop
            time_seconds: Time since charge (auto-calculated if not provided)
        """
        if time_seconds is not None:
            self._drop_time = time_seconds
        elif self._charge_time:
            self._drop_time = (datetime.now(UTC) - self._charge_time).total_seconds()
        self._drop_temp = temp
        self._set_phase(None)
        _logger.info("DROP at %.1f°C after %.0fs", temp or 0, self._drop_time or 0)

    def on_abort(self, reason: str | None = None) -> None:
        """Called when roast is aborted.

        Args:
            reason: Optional reason for abort
        """
        _logger.warning("Roast ABORTED: %s", reason or "No reason given")
        self._set_phase(None)

    def on_reset(self) -> None:
        """Called when resetting to idle state."""
        _logger.info("Roast state reset")
        self._clear_roast_data()

    def on_enter_state(self, source: State, target: State, event: str) -> None:
        """Called after any state transition.

        Args:
            source: Previous state
            target: New state
            event: Name of the transition event
        """
        _logger.debug("State transition: %s → %s (event: %s)", source.id, target.id, event)
        # Skip callback for initial state activation (source.id is empty string)
        if self._on_state_change and source.id:
            self._on_state_change(RoastState(source.id), RoastState(target.id))

    # Roast event markers (called during ROASTING state)

    def mark_turning_point(
        self,
        temp: float | None = None,
        time_seconds: float | None = None,
    ) -> None:
        """Mark the turning point (lowest BT after charge).

        Args:
            temp: Bean temperature at turning point
            time_seconds: Time since charge in seconds

        Raises:
            InvalidStateError: If not in ROASTING state
        """
        if not self.is_roasting:
            raise InvalidStateError(
                f"Cannot mark turning point in state {self.state_enum.value}. "
                "Must be in ROASTING state."
            )
        self._turning_point_time = time_seconds
        self._turning_point_temp = temp
        _logger.info("Turning point at %.1f°C after %.0fs", temp or 0, time_seconds or 0)

    def mark_dry_end(
        self,
        temp: float | None = None,
        time_seconds: float | None = None,
    ) -> None:
        """Mark the end of drying phase.

        This transitions from DRYING to MAILLARD sub-phase.

        Args:
            temp: Bean temperature at dry end
            time_seconds: Time since charge in seconds

        Raises:
            InvalidStateError: If not in ROASTING state
        """
        if not self.is_roasting:
            raise InvalidStateError(
                f"Cannot mark dry end in state {self.state_enum.value}. "
                "Must be in ROASTING state."
            )
        self._dry_end_time = time_seconds
        self._dry_end_temp_actual = temp
        self._set_phase(RoastPhase.MAILLARD)
        _logger.info("Dry end at %.1f°C after %.0fs", temp or 0, time_seconds or 0)

    def mark_first_crack(
        self,
        temp: float | None = None,
        time_seconds: float | None = None,
    ) -> None:
        """Mark the start of first crack.

        This transitions from MAILLARD to DEVELOPMENT sub-phase.

        Args:
            temp: Bean temperature at first crack
            time_seconds: Time since charge in seconds

        Raises:
            InvalidStateError: If not in ROASTING state
        """
        if not self.is_roasting:
            raise InvalidStateError(
                f"Cannot mark first crack in state {self.state_enum.value}. "
                "Must be in ROASTING state."
            )
        self._fc_start_time = time_seconds
        self._fc_start_temp = temp
        self._set_phase(RoastPhase.DEVELOPMENT)
        _logger.info("First crack at %.1f°C after %.0fs", temp or 0, time_seconds or 0)

    def mark_first_crack_end(
        self,
        temp: float | None = None,
        time_seconds: float | None = None,
    ) -> None:
        """Mark the end of first crack.

        Args:
            temp: Bean temperature at first crack end
            time_seconds: Time since charge in seconds

        Raises:
            InvalidStateError: If not in ROASTING state
        """
        if not self.is_roasting:
            raise InvalidStateError(
                f"Cannot mark first crack end in state {self.state_enum.value}. "
                "Must be in ROASTING state."
            )
        self._fc_end_time = time_seconds
        self._fc_end_temp = temp
        _logger.info("First crack ended at %.1f°C after %.0fs", temp or 0, time_seconds or 0)

    def mark_second_crack(
        self,
        temp: float | None = None,
        time_seconds: float | None = None,
    ) -> None:
        """Mark the start of second crack.

        Args:
            temp: Bean temperature at second crack
            time_seconds: Time since charge in seconds

        Raises:
            InvalidStateError: If not in ROASTING state
        """
        if not self.is_roasting:
            raise InvalidStateError(
                f"Cannot mark second crack in state {self.state_enum.value}. "
                "Must be in ROASTING state."
            )
        self._sc_start_time = time_seconds
        self._sc_start_temp = temp
        _logger.info("Second crack at %.1f°C after %.0fs", temp or 0, time_seconds or 0)

    def get_roast_summary(self) -> dict[str, Any]:
        """Get a summary of the roast data.

        Returns:
            Dictionary with all roast timing and temperature data
        """
        return {
            "state": self.state_enum.value,
            "phase": self._current_phase.name if self._current_phase else None,
            "charge_time": self._charge_time.isoformat() if self._charge_time else None,
            "charge_temp": self._charge_temp,
            "turning_point_time": self._turning_point_time,
            "turning_point_temp": self._turning_point_temp,
            "dry_end_time": self._dry_end_time,
            "dry_end_temp": self._dry_end_temp_actual,
            "fc_start_time": self._fc_start_time,
            "fc_start_temp": self._fc_start_temp,
            "fc_end_time": self._fc_end_time,
            "fc_end_temp": self._fc_end_temp,
            "sc_start_time": self._sc_start_time,
            "sc_start_temp": self._sc_start_temp,
            "drop_time": self._drop_time,
            "drop_temp": self._drop_temp,
        }

    def can_transition(self, event: str) -> bool:
        """Check if a transition is allowed from the current state.

        Args:
            event: Name of the transition event to check

        Returns:
            True if the transition is allowed
        """
        try:
            # Get the transition method
            transition = getattr(self, event, None)
            if transition is None:
                return False

            # Check if it's a valid transition from current state
            # python-statemachine will raise TransitionNotAllowed if not valid
            return hasattr(transition, "_sm_event")
        except Exception:
            return False

    def __repr__(self) -> str:
        """Return string representation of state machine."""
        phase_str = f", phase={self._current_phase.name}" if self._current_phase else ""
        return f"RoastStateMachine(state={self.state_enum.value}{phase_str})"
