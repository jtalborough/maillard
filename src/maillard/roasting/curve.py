"""Curve data processing for roast temperature logging and analysis.

This module implements temperature logging and Rate of Rise (RoR) calculation
for coffee roasting. It provides:
- Temperature logging with timestamps (monotonically increasing)
- RoR calculation (delta BT / delta time) with configurable window
- Moving-window polynomial smoothing filters
- DTR (Development Time Ratio) calculation
- Export to CurveDataSchema format

Smoothing Note:
    The smoothing filter uses pre-computed polynomial coefficients similar to
    Savitzky-Golay filtering for common window sizes. For unsupported configurations,
    it falls back to simple moving average. Edge samples retain their original values.
    This is NOT equivalent to scipy.signal.savgol_filter for all cases.

RoR Calculation Note:
    RoR values are only computed when sufficient history exists within the configured
    window. Early samples return None rather than using a shortened window, ensuring
    consistent RoR semantics for downstream controllers.

Example:
    collector = CurveCollector(sample_interval=1.5)

    # Record data during roast
    collector.record_sample(
        time_seconds=0.0,
        bt=195.0,
        et=220.0,
        heat=100,
        fan=40,
        drum=60
    )

    # Add roast events
    collector.add_event("charge", 0.0, 195.0)
    collector.add_event("turning_point", 75.0, 97.0)

    # Calculate RoR
    ror = collector.calculate_ror()

    # Calculate DTR after FC and drop
    dtr = collector.calculate_dtr(fc_start_time=450.0, drop_time=540.0)

    # Export to schema
    curve_data = collector.to_schema()
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from maillard.db.schemas import CurveDataSchema, CurveEventSchema

_logger = logging.getLogger(__name__)


# Constants
DEFAULT_SAMPLE_INTERVAL: Final[float] = 1.5  # Seconds between samples
MIN_SAMPLES_FOR_ROR: Final[int] = 2  # Minimum samples needed for RoR calculation
ROR_WINDOW_SECONDS: Final[float] = 30.0  # Time window for RoR calculation (seconds)
DEFAULT_SMOOTHING_WINDOW: Final[int] = 5  # Smoothing window size (must be odd)

# Valid event types for roast markers
VALID_EVENT_TYPES: Final[frozenset[str]] = frozenset({
    "charge",
    "turning_point",
    "dry_end",
    "fc_start",
    "fc_end",
    "sc_start",
    "sc_end",
    "drop",
})


class NonMonotonicTimeError(ValueError):
    """Raised when a sample timestamp is not strictly increasing."""


class InvalidEventTypeError(ValueError):
    """Raised when an invalid event type is provided."""


class DuplicateEventError(ValueError):
    """Raised when attempting to add a duplicate event."""


@dataclass
class CurveSample:
    """A single data point in the roast curve.

    Attributes:
        time_seconds: Time since charge in seconds
        bt: Bean temperature in Celsius
        et: Environment temperature in Celsius
        heat: Heating power percentage (0-100)
        fan: Fan speed percentage (0-100)
        drum: Drum speed percentage (0-100)
    """

    time_seconds: float
    bt: float
    et: float
    heat: int = 0
    fan: int = 0
    drum: int = 0


@dataclass
class CurveEvent:
    """A roast event marker.

    Attributes:
        type: Event type (charge, turning_point, dry_end, fc_start, fc_end, sc_start, drop)
        time_seconds: Time since charge in seconds
        temp: Bean temperature at event in Celsius
    """

    type: str
    time_seconds: float
    temp: float


@dataclass
class CurveCollector:
    """Collects and processes roast curve data.

    This class manages temperature logging during a roast, calculates Rate of Rise
    (RoR), applies smoothing filters, and exports data to the database schema format.

    Samples must be recorded with strictly increasing timestamps. Events use a
    "first wins" policy - duplicate event types are rejected.

    Attributes:
        sample_interval: Expected interval between samples in seconds
        smoothing_window: Window size for polynomial smoothing filter (must be odd, >= 3)
        samples: List of recorded temperature samples
        events: List of roast event markers
    """

    sample_interval: float = DEFAULT_SAMPLE_INTERVAL
    smoothing_window: int = DEFAULT_SMOOTHING_WINDOW
    samples: list[CurveSample] = field(default_factory=list)
    events: list[CurveEvent] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    connection_summary: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        self.smoothing_window = max(self.smoothing_window, 3)
        if self.smoothing_window % 2 == 0:
            self.smoothing_window += 1  # Must be odd

    def record_sample(
        self,
        time_seconds: float,
        bt: float,
        et: float,
        heat: int = 0,
        fan: int = 0,
        drum: int = 0,
    ) -> None:
        """Record a temperature sample.

        Samples must be recorded in strictly increasing time order. This ensures
        consistent behavior for interpolation and RoR calculations.

        Args:
            time_seconds: Time since charge in seconds (must be > previous sample)
            bt: Bean temperature in Celsius
            et: Environment temperature in Celsius
            heat: Heating power percentage (0-100)
            fan: Fan speed percentage (0-100)
            drum: Drum speed percentage (0-100)

        Raises:
            NonMonotonicTimeError: If time_seconds is not strictly greater than
                the previous sample's timestamp
        """
        # Validate monotonic time ordering
        if self.samples and time_seconds <= self.samples[-1].time_seconds:
            raise NonMonotonicTimeError(
                f"Sample time {time_seconds}s must be greater than previous "
                f"sample time {self.samples[-1].time_seconds}s"
            )

        sample = CurveSample(
            time_seconds=time_seconds,
            bt=bt,
            et=et,
            heat=max(0, min(100, heat)),
            fan=max(0, min(100, fan)),
            drum=max(0, min(100, drum)),
        )
        self.samples.append(sample)
        _logger.debug("Recorded sample at %.1fs: BT=%.1f°C, ET=%.1f°C", time_seconds, bt, et)

    def add_event(self, event_type: str, time_seconds: float, temp: float) -> None:
        """Add a roast event marker.

        Uses "first wins" semantics - if an event of the same type already exists,
        a DuplicateEventError is raised rather than silently replacing it.

        Args:
            event_type: Event type (charge, turning_point, dry_end, fc_start,
                       fc_end, sc_start, sc_end, drop)
            time_seconds: Time since charge in seconds
            temp: Bean temperature at event in Celsius

        Raises:
            InvalidEventTypeError: If event_type is not a valid event type
            DuplicateEventError: If an event of the same type already exists
        """
        if event_type not in VALID_EVENT_TYPES:
            raise InvalidEventTypeError(
                f"Invalid event type '{event_type}'. Valid types: {sorted(VALID_EVENT_TYPES)}"
            )

        # Check for duplicates (first wins policy)
        for existing in self.events:
            if existing.type == event_type:
                raise DuplicateEventError(
                    f"Event '{event_type}' already exists at {existing.time_seconds}s. "
                    "Use clear_event() first if you need to replace it."
                )

        event = CurveEvent(type=event_type, time_seconds=time_seconds, temp=temp)
        self.events.append(event)
        _logger.info("Added event '%s' at %.1fs (%.1f°C)", event_type, time_seconds, temp)

    def clear_event(self, event_type: str) -> bool:
        """Remove an event by type.

        Args:
            event_type: Event type to remove

        Returns:
            True if an event was removed, False if no matching event found
        """
        for i, event in enumerate(self.events):
            if event.type == event_type:
                self.events.pop(i)
                _logger.debug("Cleared event '%s'", event_type)
                return True
        return False

    def get_event(self, event_type: str) -> CurveEvent | None:
        """Get an event by type.

        Args:
            event_type: Event type to find

        Returns:
            The event if found, None otherwise
        """
        for event in self.events:
            if event.type == event_type:
                return event
        return None

    def clear(self) -> None:
        """Clear all recorded samples and events."""
        self.samples.clear()
        self.events.clear()
        self.gaps.clear()
        self.connection_summary.clear()
        _logger.debug("Cleared curve data")

    @property
    def duration(self) -> float:
        """Total duration of recorded data in seconds."""
        if not self.samples:
            return 0.0
        return self.samples[-1].time_seconds - self.samples[0].time_seconds

    @property
    def sample_count(self) -> int:
        """Number of recorded samples."""
        return len(self.samples)

    def get_bt_at_time(self, time_seconds: float) -> float | None:
        """Get bean temperature at a specific time (interpolated if needed).

        Args:
            time_seconds: Time since charge in seconds

        Returns:
            Interpolated bean temperature, or None if no data
        """
        if not self.samples:
            return None

        # Find surrounding samples
        for i, sample in enumerate(self.samples):
            if sample.time_seconds >= time_seconds:
                if i == 0:
                    return sample.bt
                # Linear interpolation
                prev = self.samples[i - 1]
                t_ratio = (time_seconds - prev.time_seconds) / (
                    sample.time_seconds - prev.time_seconds
                )
                return prev.bt + t_ratio * (sample.bt - prev.bt)

        # Time is beyond last sample
        return self.samples[-1].bt

    def calculate_ror(
        self,
        smoothing: bool = True,
        window_seconds: float = ROR_WINDOW_SECONDS,
    ) -> list[float | None]:
        """Calculate Rate of Rise (RoR) for all samples.

        RoR is calculated as the change in bean temperature over a time window,
        expressed in °C/minute.

        The algorithm looks back 'window_seconds' from each sample and calculates
        the temperature delta, then converts to per-minute rate. If smoothing is
        enabled, the BT values are smoothed first using the polynomial filter.

        Unlike some implementations, this returns None for early samples where
        sufficient history doesn't exist within the window, rather than using
        a shortened window. This ensures consistent RoR semantics.

        Args:
            smoothing: Whether to apply smoothing to BT before calculation
            window_seconds: Time window in seconds for RoR calculation

        Returns:
            List of RoR values (°C/min) for each sample, with None for samples
            where RoR cannot be calculated (insufficient history within window)
        """
        if len(self.samples) < MIN_SAMPLES_FOR_ROR:
            return [None] * len(self.samples)

        # Get BT values (possibly smoothed)
        bt_values = self.smooth_bt() if smoothing else [s.bt for s in self.samples]

        times = [s.time_seconds for s in self.samples]
        ror_values: list[float | None] = []

        for i, (current_time, current_bt) in enumerate(zip(times, bt_values, strict=True)):
            if i == 0:
                ror_values.append(None)
                continue

            # Find the sample closest to window_seconds before current
            target_time = current_time - window_seconds
            prev_idx: int | None = None
            prev_time: float = 0.0

            # Search for sample at or before target_time
            for j in range(i - 1, -1, -1):
                if times[j] <= target_time:
                    prev_idx = j
                    prev_time = times[j]
                    break

            # If no sample exists within window, return None (strict behavior)
            if prev_idx is None:
                ror_values.append(None)
                continue

            # Calculate RoR (°C/min)
            time_delta = current_time - prev_time
            if time_delta <= 0:
                ror_values.append(None)
                continue

            temp_delta = current_bt - bt_values[prev_idx]
            ror = (temp_delta / time_delta) * 60.0  # Convert to °C/min
            ror_values.append(round(ror, 2))

        return ror_values

    def calculate_instant_ror(self, index: int = -1) -> float | None:
        """Calculate instantaneous RoR at a specific sample.

        Uses the difference between the sample and its immediate predecessor.

        Args:
            index: Sample index (default: -1 for most recent)

        Returns:
            Instantaneous RoR in °C/min, or None if cannot be calculated
        """
        if len(self.samples) < MIN_SAMPLES_FOR_ROR:
            return None

        # Normalize negative index
        if index < 0:
            index = len(self.samples) + index

        if index <= 0 or index >= len(self.samples):
            return None

        current = self.samples[index]
        previous = self.samples[index - 1]

        time_delta = current.time_seconds - previous.time_seconds
        if time_delta <= 0:
            return None

        temp_delta = current.bt - previous.bt
        return round((temp_delta / time_delta) * 60.0, 2)

    def smooth_bt(
        self,
        window: int | None = None,
        polynomial_order: int = 2,
    ) -> list[float]:
        """Apply moving-window polynomial smoothing to BT values.

        This uses pre-computed coefficients similar to Savitzky-Golay filtering
        for common window sizes (5, 7, 9 with order 2-3). For unsupported
        configurations, it falls back to simple moving average.

        Note: This is NOT equivalent to scipy.signal.savgol_filter for all cases.
        Edge samples retain their original values.

        Args:
            window: Window size for filter (must be odd, default: self.smoothing_window)
            polynomial_order: Polynomial order for fitting (default: 2)

        Returns:
            List of smoothed BT values
        """
        bt_values = [s.bt for s in self.samples]

        if len(bt_values) < 3:
            return bt_values.copy()

        if window is None:
            window = self.smoothing_window

        # Ensure window is valid
        if window > len(bt_values):
            window = len(bt_values) if len(bt_values) % 2 == 1 else len(bt_values) - 1
        window = max(window, 3)
        if window % 2 == 0:
            window += 1

        if polynomial_order >= window:
            polynomial_order = window - 1

        return self._apply_smoothing_filter(bt_values, window, polynomial_order)

    def _apply_smoothing_filter(
        self,
        data: Sequence[float],
        window: int,
        order: int,
    ) -> list[float]:
        """Apply polynomial smoothing filter to data.

        Uses pre-computed coefficients for common window/order combinations.
        Falls back to simple moving average for unsupported configurations.

        Args:
            data: Input data sequence
            window: Window size (must be odd)
            order: Polynomial order

        Returns:
            Smoothed data as list
        """
        n = len(data)
        if n < window:
            return list(data)

        half_window = window // 2
        result = list(data)  # Copy original

        # Pre-compute coefficients for smoothing
        coeffs = self._get_smoothing_coefficients(window, order)

        # Apply filter (handle edges by keeping original values)
        for i in range(n):
            start = i - half_window
            end = i + half_window + 1

            # Skip edge cases - keep original values
            if start < 0 or end > n:
                continue

            # Apply convolution
            total = 0.0
            for j, c in enumerate(coeffs):
                total += c * data[start + j]
            result[i] = total

        return result

    def _get_smoothing_coefficients(self, window: int, order: int) -> list[float]:
        """Get smoothing filter coefficients.

        Uses pre-computed coefficients for common polynomial smoothing cases.
        Falls back to simple moving average for unsupported configurations.

        Args:
            window: Window size
            order: Polynomial order

        Returns:
            List of filter coefficients (normalized, sum to 1.0)
        """
        # Pre-computed coefficients for common cases
        # These approximate Savitzky-Golay smoothing coefficients
        if order == 2:
            if window == 5:
                # [-3, 12, 17, 12, -3] / 35
                return [-0.086, 0.343, 0.486, 0.343, -0.086]
            if window == 7:
                # [-2, 3, 6, 7, 6, 3, -2] / 21
                return [-0.095, 0.143, 0.286, 0.333, 0.286, 0.143, -0.095]
            if window == 9:
                return [-0.090, 0.060, 0.169, 0.234, 0.253, 0.234, 0.169, 0.060, -0.090]

        if order == 3:
            if window == 5:
                return [-0.086, 0.343, 0.486, 0.343, -0.086]
            if window == 7:
                return [-0.048, 0.095, 0.238, 0.429, 0.238, 0.095, -0.048]

        # Fallback: simple moving average
        _logger.debug(
            "No pre-computed coefficients for window=%d, order=%d; using moving average",
            window,
            order,
        )
        return [1.0 / window] * window

    def calculate_dtr(
        self,
        fc_start_time: float | None = None,
        drop_time: float | None = None,
    ) -> float | None:
        """Calculate Development Time Ratio (DTR).

        DTR = (drop_time - fc_start_time) / drop_time * 100

        This measures what percentage of the total roast time was spent
        in the development phase (after first crack).

        Args:
            fc_start_time: Time of first crack start (seconds from charge).
                          If None, looks for 'fc_start' event.
            drop_time: Time of drop (seconds from charge).
                      If None, looks for 'drop' event.

        Returns:
            DTR as percentage (0-100), or None if insufficient data
        """
        # Try to get times from events if not provided (first match wins)
        if fc_start_time is None:
            event = self.get_event("fc_start")
            if event:
                fc_start_time = event.time_seconds

        if drop_time is None:
            event = self.get_event("drop")
            if event:
                drop_time = event.time_seconds

        # Validate we have both times
        if fc_start_time is None or drop_time is None:
            _logger.warning("Cannot calculate DTR: missing fc_start_time or drop_time")
            return None

        if drop_time <= 0:
            _logger.warning("Cannot calculate DTR: drop_time must be positive")
            return None

        if fc_start_time >= drop_time:
            _logger.warning("Cannot calculate DTR: fc_start_time must be before drop_time")
            return None

        development_time = drop_time - fc_start_time
        dtr = (development_time / drop_time) * 100.0
        return round(dtr, 2)

    def calculate_phase_times(self) -> dict[str, float | None]:
        """Calculate duration of each roast phase.

        Uses "first wins" semantics for event lookup - the first event of each
        type recorded is used.

        Returns:
            Dictionary with phase durations in seconds:
            - drying_time: charge to dry_end
            - maillard_time: dry_end to fc_start
            - development_time: fc_start to drop
            - total_time: charge to drop
        """
        # Get event times using get_event (which returns first match)
        charge_event = self.get_event("charge")
        dry_end_event = self.get_event("dry_end")
        fc_start_event = self.get_event("fc_start")
        drop_event = self.get_event("drop")

        charge_time = charge_event.time_seconds if charge_event else 0.0
        dry_end_time = dry_end_event.time_seconds if dry_end_event else None
        fc_start_time = fc_start_event.time_seconds if fc_start_event else None
        drop_time = drop_event.time_seconds if drop_event else None

        result: dict[str, float | None] = {
            "drying_time": None,
            "maillard_time": None,
            "development_time": None,
            "total_time": None,
        }

        if dry_end_time is not None:
            result["drying_time"] = dry_end_time - charge_time

        if dry_end_time is not None and fc_start_time is not None:
            result["maillard_time"] = fc_start_time - dry_end_time

        if fc_start_time is not None and drop_time is not None:
            result["development_time"] = drop_time - fc_start_time

        if drop_time is not None:
            result["total_time"] = drop_time - charge_time

        return result

    def get_statistics(self, use_smoothed_ror: bool = True) -> dict[str, float | None]:
        """Calculate statistics for the roast curve.

        Args:
            use_smoothed_ror: If True (default), RoR statistics are based on
                smoothed BT values. Set to False for raw RoR values.

        Returns:
            Dictionary with curve statistics:
            - min_bt: Minimum bean temperature (turning point)
            - max_bt: Maximum bean temperature
            - min_bt_time: Time of minimum BT
            - max_bt_time: Time of maximum BT
            - avg_ror: Average RoR after turning point (smoothed by default)
            - max_ror: Maximum RoR value
            - min_ror: Minimum RoR value (after turning point)
        """
        if not self.samples:
            return {
                "min_bt": None,
                "max_bt": None,
                "min_bt_time": None,
                "max_bt_time": None,
                "avg_ror": None,
                "max_ror": None,
                "min_ror": None,
            }

        bt_values = [s.bt for s in self.samples]
        times = [s.time_seconds for s in self.samples]

        min_bt = min(bt_values)
        max_bt = max(bt_values)
        min_bt_idx = bt_values.index(min_bt)
        max_bt_idx = bt_values.index(max_bt)

        ror_values = self.calculate_ror(smoothing=use_smoothed_ror)

        # Filter RoR values after turning point (skip initial cooling)
        valid_ror: list[float] = []
        for i, ror in enumerate(ror_values):
            if ror is not None and i >= min_bt_idx:
                valid_ror.append(ror)

        return {
            "min_bt": round(min_bt, 2),
            "max_bt": round(max_bt, 2),
            "min_bt_time": round(times[min_bt_idx], 1),
            "max_bt_time": round(times[max_bt_idx], 1),
            "avg_ror": round(sum(valid_ror) / len(valid_ror), 2) if valid_ror else None,
            "max_ror": round(max(valid_ror), 2) if valid_ror else None,
            "min_ror": round(min(valid_ror), 2) if valid_ror else None,
        }

    def detect_turning_point(self, max_search_time: float = 120.0) -> tuple[float, float] | None:
        """Auto-detect the turning point (lowest BT after charge).

        The turning point is the lowest bean temperature reached after
        the initial drop when cold beans are loaded.

        Args:
            max_search_time: Maximum time (seconds) from first sample to search
                            for turning point. Default 120s (2 minutes).

        Returns:
            Tuple of (time_seconds, temp) or None if not enough data
        """
        if len(self.samples) < 3:
            return None

        # Find the minimum BT within the search window using timestamps directly
        first_time = self.samples[0].time_seconds
        search_end_time = first_time + max_search_time

        min_bt = float("inf")
        min_idx = 0

        for i, sample in enumerate(self.samples):
            if sample.time_seconds > search_end_time:
                break
            if sample.bt < min_bt:
                min_bt = sample.bt
                min_idx = i

        # Verify this is a valid turning point (temps should rise after)
        if min_idx < len(self.samples) - 1 and self.samples[min_idx + 1].bt > min_bt:
            return (self.samples[min_idx].time_seconds, round(min_bt, 2))

        return None

    def to_schema(self) -> CurveDataSchema:
        """Export curve data to database schema format.

        Note: Event times are rounded to integers in the schema for storage
        efficiency. Use from_schema() to restore, which will lose sub-second
        precision for event times.

        Returns:
            CurveDataSchema with all collected data
        """
        ror_values = self.calculate_ror()

        return CurveDataSchema(
            gaps=self.gaps,
            connection_summary=self.connection_summary,
            time_seconds=[s.time_seconds for s in self.samples],
            bt=[round(s.bt, 2) for s in self.samples],
            et=[round(s.et, 2) for s in self.samples],
            bt_ror=ror_values,
            heat=[s.heat for s in self.samples],
            fan=[s.fan for s in self.samples],
            drum=[s.drum for s in self.samples],
            events=[
                CurveEventSchema(
                    time=int(e.time_seconds),
                    type=e.type,
                    temp=round(e.temp, 2),
                )
                for e in self.events
            ],
        )

    @classmethod
    def from_schema(cls, schema: CurveDataSchema) -> CurveCollector:
        """Create CurveCollector from database schema.

        Note: RoR values from the schema are ignored and will be recalculated.
        Event times lose sub-second precision due to integer storage in schema.

        Args:
            schema: CurveDataSchema with curve data

        Returns:
            New CurveCollector instance with data loaded
        """
        collector = cls()
        collector.gaps = [gap.copy() for gap in schema.gaps]
        collector.connection_summary = schema.connection_summary.copy()

        # Load samples directly to bypass time validation (data is from storage)
        for i in range(len(schema.time_seconds)):
            collector.samples.append(
                CurveSample(
                    time_seconds=schema.time_seconds[i],
                    bt=schema.bt[i] if i < len(schema.bt) else 0.0,
                    et=schema.et[i] if i < len(schema.et) else 0.0,
                    heat=schema.heat[i] if i < len(schema.heat) else 0,
                    fan=schema.fan[i] if i < len(schema.fan) else 0,
                    drum=schema.drum[i] if i < len(schema.drum) else 0,
                )
            )

        # Load events directly (bypass validation for restored data)
        for event in schema.events:
            collector.events.append(
                CurveEvent(
                    type=event.type,
                    time_seconds=float(event.time),
                    temp=event.temp,
                )
            )

        return collector

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"CurveCollector(samples={len(self.samples)}, "
            f"events={len(self.events)}, "
            f"duration={self.duration:.1f}s)"
        )


__all__ = [
    "CurveCollector",
    "CurveEvent",
    "CurveSample",
    "DEFAULT_SAMPLE_INTERVAL",
    "DEFAULT_SMOOTHING_WINDOW",
    "DuplicateEventError",
    "InvalidEventTypeError",
    "MIN_SAMPLES_FOR_ROR",
    "NonMonotonicTimeError",
    "ROR_WINDOW_SECONDS",
    "VALID_EVENT_TYPES",
]
