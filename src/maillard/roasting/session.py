"""Roast session lifecycle management.

Bundles RoastStateMachine, CurveCollector, and RoastContext into a single
session object that manages the full lifecycle of a roast from charge to
drop (or abort).

A RoastSession is created at charge time and cleared when the roast
completes or aborts. During the roast, it subscribes to EventBus
StateChangeEvents to record temperature samples into the CurveCollector.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from maillard.core.events import StateChangeEvent
from maillard.roasting.curve import CurveCollector
from maillard.roasting.state import RoastPhase, RoastState, RoastStateMachine

if TYPE_CHECKING:
    from maillard.core.events import EventBus, UnsubscribeFunc
    from maillard.db.schemas import CurveDataSchema

_logger = logging.getLogger(__name__)


class RoastSession:
    """Manages the lifecycle of a single roast.

    Bundles together:
    - RoastStateMachine: enforces valid state transitions
    - CurveCollector: records temperature samples and events
    - Reference fields: roast_id, profile_id, coffee_id

    The session subscribes to EventBus StateChangeEvents to record
    temperature samples during the ROASTING state.

    Usage:
        session = RoastSession.start(
            event_bus=bus,
            charge_temp=195.0,
            coffee_id="abc",
            profile_id="xyz",
        )
        # ... roast lifecycle ...
        curve_data = session.finalize()
    """

    def __init__(
        self,
        machine: RoastStateMachine,
        collector: CurveCollector,
        event_bus: EventBus,
        *,
        roast_id: str | None = None,
        profile_id: str | None = None,
        coffee_id: str | None = None,
        batch_size_grams: int | None = None,
    ) -> None:
        self.connection_events: list[dict] = []
        self._gap_start: float | None = None
        self._gap_reason = "stale"
        self.machine = machine
        self.collector = collector
        self.event_bus = event_bus
        self.roast_id = roast_id
        self.profile_id = profile_id
        self.coffee_id = coffee_id
        self.batch_size_grams = batch_size_grams
        self._unsubscribe: UnsubscribeFunc | None = None

    @classmethod
    def start(
        cls,
        event_bus: EventBus,
        charge_temp: float,
        *,
        roast_id: str | None = None,
        coffee_id: str | None = None,
        profile_id: str | None = None,
        batch_size_grams: int | None = None,
        machine: RoastStateMachine | None = None,
    ) -> RoastSession:
        """Create and start a new roast session.

        Transitions the state machine to ROASTING via skip_to_ready() + charge()
        (or just charge() if already in READY_TO_CHARGE).

        Args:
            event_bus: EventBus for subscribing to state changes
            charge_temp: Bean temperature at charge
            roast_id: Database roast record ID
            coffee_id: Coffee being roasted
            profile_id: Profile being followed
            batch_size_grams: Batch size
            machine: Optional pre-existing state machine (e.g. from preheat)

        Returns:
            Started RoastSession in ROASTING state
        """
        if machine is None:
            machine = RoastStateMachine()

        # Transition to ROASTING
        current_state = machine.state_enum
        if current_state == RoastState.IDLE:
            machine.skip_to_ready()
            machine.charge(temp=charge_temp)
        elif current_state == RoastState.READY_TO_CHARGE:
            machine.charge(temp=charge_temp)
        elif current_state == RoastState.PREHEATING:
            machine.mark_ready()
            machine.charge(temp=charge_temp)
        else:
            from maillard.roasting.state import InvalidStateError

            raise InvalidStateError(
                f"Cannot start roast session from state {current_state.value}"
            )

        collector = CurveCollector()
        # Record charge event
        collector.add_event("charge", 0.0, charge_temp)

        session = cls(
            machine=machine,
            collector=collector,
            event_bus=event_bus,
            roast_id=roast_id,
            profile_id=profile_id,
            coffee_id=coffee_id,
            batch_size_grams=batch_size_grams,
        )

        # Subscribe to state change events for sample recording
        session._unsubscribe = event_bus.on(StateChangeEvent, session._on_state_change)
        _logger.info("Roast session started (roast_id=%s)", roast_id)
        return session

    def _on_state_change(self, event: StateChangeEvent) -> None:
        """Handle incoming StateChangeEvent to record temperature samples."""
        if not self.machine.is_roasting:
            return

        elapsed = self.machine.elapsed_time
        if elapsed is None:
            return

        state = event.state
        bt = state.get("BT")
        et = state.get("ET")
        if event.stale or bt is None or et is None:
            if self._gap_start is None:
                self._gap_start = elapsed
            if not event.connected:
                self._gap_reason = "disconnected"
            return
        self._close_gap(elapsed)

        try:
            self.collector.record_sample(
                time_seconds=round(elapsed, 1),
                bt=bt,
                et=et,
                heat=state.get("HP", 0),
                fan=state.get("FC", 0),
                drum=state.get("RC", 0),
            )
        except Exception:
            _logger.debug("Failed to record sample", exc_info=True)

    def record_connection_event(self, event: dict) -> None:
        self.connection_events.append({**event, "elapsed": self.elapsed_time})
        if event["type"] in {"closed", "forced_close"}:
            if self._gap_start is None:
                self._gap_start = self.elapsed_time
            self._gap_reason = "disconnected"

    def _close_gap(self, elapsed: float) -> None:
        if self._gap_start is not None:
            end = max(elapsed, self._gap_start)
            self.collector.gaps.append({"start": self._gap_start, "end": end,
                                       "duration": end - self._gap_start,
                                       "reason": self._gap_reason})
            self._gap_start = None
            self._gap_reason = "stale"

    @property
    def is_active(self) -> bool:
        """Whether the roast session is in an active state."""
        return self.machine.is_active

    @property
    def current_phase(self) -> RoastPhase | None:
        """Current sub-phase within ROASTING."""
        return self.machine.current_phase

    @property
    def state_enum(self) -> RoastState:
        """Current state machine state."""
        return self.machine.state_enum

    @property
    def elapsed_time(self) -> float | None:
        """Seconds since charge."""
        return self.machine.elapsed_time

    @property
    def latest_ror(self) -> float | None:
        """Most recent Rate of Rise value (smoothed, 30s window)."""
        ror_values = self.collector.calculate_ror(smoothing=True, window_seconds=30)
        if ror_values:
            # Return most recent non-None value
            for v in reversed(ror_values):
                if v is not None:
                    return v
        return None

    @property
    def sample_count(self) -> int:
        """Number of recorded temperature samples."""
        return self.collector.sample_count

    def mark_event(
        self,
        event_type: str,
        temp: float,
        time_seconds: float,
    ) -> None:
        """Mark a roast event on both the state machine and curve collector.

        Args:
            event_type: Event type string (turning_point, dry_end, etc.)
            temp: Bean temperature at event
            time_seconds: Seconds since charge
        """
        # State machine event markers
        marker_map = {
            "turning_point": self.machine.mark_turning_point,
            "dry_end": self.machine.mark_dry_end,
            "fc_start": self.machine.mark_first_crack,
            "fc_end": self.machine.mark_first_crack_end,
            "sc_start": self.machine.mark_second_crack,
        }
        marker = marker_map.get(event_type)
        if marker:
            marker(temp=temp, time_seconds=time_seconds)

        # Record in curve collector
        self.collector.add_event(event_type, time_seconds, temp)

    def finalize(self, status: str = "completed") -> CurveDataSchema:
        """Finalize the session: unsubscribe from events and export curve data.

        Args:
            status: Final status ("completed" or "aborted")

        Returns:
            CurveDataSchema with all collected curve data
        """
        # Unsubscribe from event bus
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

        self._close_gap(self.elapsed_time or 0.0)
        self.collector.connection_summary = {
            "stalls": sum(e["type"] == "stall" for e in self.connection_events),
            "disconnects": sum(e["type"] == "closed" for e in self.connection_events),
            "gap_seconds": sum(g["duration"] for g in self.collector.gaps),
        }
        curve_data = self.collector.to_schema()
        _logger.info(
            "Roast session finalized (status=%s, samples=%d, events=%d)",
            status,
            self.collector.sample_count,
            len(self.collector.events),
        )
        return curve_data

    def get_curve_data_json(self) -> str:
        """Export curve data as JSON string for DB storage."""
        schema = self.collector.to_schema()
        return schema.model_dump_json()

    def __repr__(self) -> str:
        return (
            f"RoastSession(state={self.state_enum.value}, "
            f"roast_id={self.roast_id}, "
            f"samples={self.sample_count})"
        )


__all__ = [
    "RoastSession",
]
