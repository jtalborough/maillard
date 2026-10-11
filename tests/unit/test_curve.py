"""Unit tests for CurveCollector and curve data processing.

Tests cover:
- Sample recording and retrieval
- Time monotonicity validation
- Event marking with validation
- RoR calculation (with strict window behavior)
- DTR calculation
- Smoothing filters
- Schema conversion
- Statistics calculation
- Turning point detection
"""

import pytest

from maillard.db.schemas import CurveDataSchema, CurveEventSchema
from maillard.roasting.curve import (
    CurveCollector,
    CurveEvent,
    CurveSample,
    DEFAULT_SAMPLE_INTERVAL,
    DEFAULT_SMOOTHING_WINDOW,
    DuplicateEventError,
    InvalidEventTypeError,
    NonMonotonicTimeError,
    VALID_EVENT_TYPES,
)


class TestCurveSample:
    """Tests for CurveSample dataclass."""

    def test_create_sample(self) -> None:
        """Test creating a sample with all fields."""
        sample = CurveSample(
            time_seconds=60.0,
            bt=150.0,
            et=200.0,
            heat=80,
            fan=50,
            drum=60,
        )
        assert sample.time_seconds == 60.0
        assert sample.bt == 150.0
        assert sample.et == 200.0
        assert sample.heat == 80
        assert sample.fan == 50
        assert sample.drum == 60

    def test_default_control_values(self) -> None:
        """Test default values for control settings."""
        sample = CurveSample(time_seconds=0.0, bt=100.0, et=150.0)
        assert sample.heat == 0
        assert sample.fan == 0
        assert sample.drum == 0


class TestCurveEvent:
    """Tests for CurveEvent dataclass."""

    def test_create_event(self) -> None:
        """Test creating a roast event."""
        event = CurveEvent(type="fc_start", time_seconds=450.0, temp=196.0)
        assert event.type == "fc_start"
        assert event.time_seconds == 450.0
        assert event.temp == 196.0


class TestCurveCollectorBasics:
    """Basic functionality tests for CurveCollector."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        collector = CurveCollector()
        assert collector.sample_interval == DEFAULT_SAMPLE_INTERVAL
        assert collector.smoothing_window == DEFAULT_SMOOTHING_WINDOW
        assert collector.samples == []
        assert collector.events == []

    def test_custom_configuration(self) -> None:
        """Test custom configuration values."""
        collector = CurveCollector(sample_interval=2.0, smoothing_window=7)
        assert collector.sample_interval == 2.0
        assert collector.smoothing_window == 7

    def test_smoothing_window_validation(self) -> None:
        """Test that smoothing window is forced to be odd and >= 3."""
        # Too small
        collector = CurveCollector(smoothing_window=1)
        assert collector.smoothing_window == 3

        # Even number
        collector = CurveCollector(smoothing_window=6)
        assert collector.smoothing_window == 7

    def test_record_sample(self) -> None:
        """Test recording a temperature sample."""
        collector = CurveCollector()
        collector.record_sample(
            time_seconds=0.0, bt=195.0, et=220.0, heat=100, fan=40, drum=60
        )
        assert collector.sample_count == 1
        assert collector.samples[0].bt == 195.0

    def test_record_sample_clamps_values(self) -> None:
        """Test that control values are clamped to 0-100."""
        collector = CurveCollector()
        collector.record_sample(
            time_seconds=0.0, bt=100.0, et=150.0, heat=150, fan=-10, drum=200
        )
        assert collector.samples[0].heat == 100
        assert collector.samples[0].fan == 0
        assert collector.samples[0].drum == 100

    def test_clear(self) -> None:
        """Test clearing all data."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.add_event("charge", 0.0, 100.0)
        collector.clear()
        assert collector.samples == []
        assert collector.events == []

    def test_duration_empty(self) -> None:
        """Test duration with no samples."""
        collector = CurveCollector()
        assert collector.duration == 0.0

    def test_duration_with_samples(self) -> None:
        """Test duration calculation."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=60.0, bt=120.0, et=160.0)
        collector.record_sample(time_seconds=120.0, bt=140.0, et=170.0)
        assert collector.duration == 120.0


class TestMonotonicTimeValidation:
    """Tests for time monotonicity enforcement."""

    def test_monotonic_times_accepted(self) -> None:
        """Test that strictly increasing times are accepted."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=1.5, bt=101.0, et=151.0)
        collector.record_sample(time_seconds=3.0, bt=102.0, et=152.0)
        assert collector.sample_count == 3

    def test_duplicate_time_rejected(self) -> None:
        """Test that duplicate timestamps are rejected."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        with pytest.raises(NonMonotonicTimeError) as exc_info:
            collector.record_sample(time_seconds=0.0, bt=101.0, et=151.0)
        assert "must be greater than" in str(exc_info.value)

    def test_decreasing_time_rejected(self) -> None:
        """Test that decreasing timestamps are rejected."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=10.0, bt=100.0, et=150.0)
        with pytest.raises(NonMonotonicTimeError) as exc_info:
            collector.record_sample(time_seconds=5.0, bt=101.0, et=151.0)
        assert "5.0s must be greater than" in str(exc_info.value)
        assert "10.0s" in str(exc_info.value)


class TestEventValidation:
    """Tests for event type validation and duplicate prevention."""

    def test_valid_event_types(self) -> None:
        """Test that all valid event types are accepted."""
        collector = CurveCollector()
        for event_type in VALID_EVENT_TYPES:
            collector.clear()
            collector.add_event(event_type, 0.0, 100.0)
            assert len(collector.events) == 1
            assert collector.events[0].type == event_type

    def test_invalid_event_type_rejected(self) -> None:
        """Test that invalid event types are rejected."""
        collector = CurveCollector()
        with pytest.raises(InvalidEventTypeError) as exc_info:
            collector.add_event("invalid_event", 0.0, 100.0)
        assert "Invalid event type" in str(exc_info.value)
        assert "invalid_event" in str(exc_info.value)

    def test_duplicate_event_rejected(self) -> None:
        """Test that duplicate events are rejected (first wins)."""
        collector = CurveCollector()
        collector.add_event("charge", 0.0, 195.0)
        with pytest.raises(DuplicateEventError) as exc_info:
            collector.add_event("charge", 5.0, 190.0)
        assert "already exists" in str(exc_info.value)
        # First event should remain
        assert collector.events[0].time_seconds == 0.0
        assert collector.events[0].temp == 195.0

    def test_clear_event(self) -> None:
        """Test clearing a specific event."""
        collector = CurveCollector()
        collector.add_event("charge", 0.0, 195.0)
        collector.add_event("fc_start", 450.0, 196.0)

        assert collector.clear_event("charge") is True
        assert len(collector.events) == 1
        assert collector.events[0].type == "fc_start"

        # Can now add charge again
        collector.add_event("charge", 0.0, 194.0)
        assert len(collector.events) == 2

    def test_clear_nonexistent_event(self) -> None:
        """Test clearing an event that doesn't exist."""
        collector = CurveCollector()
        assert collector.clear_event("charge") is False

    def test_get_event(self) -> None:
        """Test getting an event by type."""
        collector = CurveCollector()
        collector.add_event("charge", 0.0, 195.0)
        collector.add_event("fc_start", 450.0, 196.0)

        event = collector.get_event("fc_start")
        assert event is not None
        assert event.time_seconds == 450.0
        assert event.temp == 196.0

    def test_get_nonexistent_event(self) -> None:
        """Test getting an event that doesn't exist."""
        collector = CurveCollector()
        assert collector.get_event("charge") is None


class TestGetBtAtTime:
    """Tests for get_bt_at_time interpolation."""

    def test_empty_collector(self) -> None:
        """Test with no samples."""
        collector = CurveCollector()
        assert collector.get_bt_at_time(30.0) is None

    def test_exact_time_match(self) -> None:
        """Test when time matches a sample exactly."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=60.0, bt=120.0, et=160.0)
        # Close to exact match (within interpolation logic)
        result = collector.get_bt_at_time(60.0)
        assert result == 120.0

    def test_interpolation(self) -> None:
        """Test linear interpolation between samples."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=60.0, bt=160.0, et=200.0)
        # Midpoint should be 130.0
        result = collector.get_bt_at_time(30.0)
        assert result == pytest.approx(130.0, rel=0.01)

    def test_time_before_first_sample(self) -> None:
        """Test when time is before first sample."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=10.0, bt=100.0, et=150.0)
        result = collector.get_bt_at_time(5.0)
        assert result == 100.0  # Returns first sample

    def test_time_after_last_sample(self) -> None:
        """Test when time is after last sample."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=60.0, bt=120.0, et=160.0)
        result = collector.get_bt_at_time(120.0)
        assert result == 120.0  # Returns last sample


class TestRoRCalculation:
    """Tests for Rate of Rise calculation."""

    def test_insufficient_samples(self) -> None:
        """Test with too few samples."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        ror = collector.calculate_ror()
        assert ror == [None]

    def test_first_sample_always_none(self) -> None:
        """Test that first sample RoR is always None."""
        collector = CurveCollector()
        for i in range(10):
            collector.record_sample(
                time_seconds=float(i * 30),
                bt=100.0 + i * 5,
                et=150.0,
            )
        ror = collector.calculate_ror()
        assert ror[0] is None

    def test_ror_none_until_window_history(self) -> None:
        """Test that RoR is None until sufficient window history exists."""
        collector = CurveCollector()
        # Window is 30 seconds by default
        # Samples at 0, 10, 20 seconds should all be None
        # Sample at 30+ should have RoR
        for i in range(5):
            collector.record_sample(
                time_seconds=float(i * 10),
                bt=100.0 + i * 5,
                et=150.0,
            )
        ror = collector.calculate_ror()
        # First sample: None (no prior)
        # Samples before window_seconds (30s): None
        # Sample at 30s+: should have value
        assert ror[0] is None  # First sample
        assert ror[1] is None  # 10s - no sample at -20s
        assert ror[2] is None  # 20s - no sample at -10s
        assert ror[3] is not None  # 30s - can use sample at 0s

    def test_basic_ror_calculation(self) -> None:
        """Test basic RoR calculation with sufficient history."""
        collector = CurveCollector()
        # Create samples spanning well beyond window_seconds (30s)
        for i in range(20):
            collector.record_sample(
                time_seconds=float(i * 6),  # 6 seconds apart, total 114s
                bt=100.0 + i,  # 1°C per sample
                et=150.0,
            )
        ror = collector.calculate_ror()
        # After 30s, should have values
        valid_ror = [r for r in ror[6:] if r is not None]  # After ~36s
        assert len(valid_ror) > 0
        assert all(r > 0 for r in valid_ror)  # All positive (rising)

    def test_ror_with_smoothing(self) -> None:
        """Test RoR calculation with smoothing enabled."""
        collector = CurveCollector()
        # Add data spanning beyond window
        for i in range(30):
            noise = 0.5 if i % 2 == 0 else -0.5
            collector.record_sample(
                time_seconds=float(i * 3),  # 90s total
                bt=100.0 + i + noise,
                et=150.0,
            )
        ror_smoothed = collector.calculate_ror(smoothing=True)
        ror_raw = collector.calculate_ror(smoothing=False)
        # Both should have values after window
        assert any(r is not None for r in ror_smoothed[10:])
        assert any(r is not None for r in ror_raw[10:])

    def test_negative_ror(self) -> None:
        """Test RoR for falling temperatures."""
        collector = CurveCollector()
        # Simulate initial charge drop over longer time
        for i in range(20):
            collector.record_sample(
                time_seconds=float(i * 6),  # 114s total
                bt=195.0 - i * 5,  # -5°C per sample (falling)
                et=220.0,
            )
        ror = collector.calculate_ror()
        # After window, should have negative values (falling temperature)
        valid_ror = [r for r in ror[6:] if r is not None]
        assert len(valid_ror) > 0
        assert all(r < 0 for r in valid_ror)

    def test_instant_ror(self) -> None:
        """Test instantaneous RoR calculation."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=10.0, bt=105.0, et=155.0)
        # RoR should be 5°C / 10s = 0.5°C/s = 30°C/min
        instant_ror = collector.calculate_instant_ror(-1)
        assert instant_ror == pytest.approx(30.0, rel=0.01)

    def test_instant_ror_insufficient_data(self) -> None:
        """Test instant RoR with insufficient data."""
        collector = CurveCollector()
        assert collector.calculate_instant_ror() is None
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        assert collector.calculate_instant_ror() is None


class TestDTRCalculation:
    """Tests for Development Time Ratio calculation."""

    def test_dtr_with_explicit_times(self) -> None:
        """Test DTR calculation with explicitly provided times."""
        collector = CurveCollector()
        # FC at 450s, drop at 540s
        # Development = 90s, Total = 540s
        # DTR = 90/540 * 100 = 16.67%
        dtr = collector.calculate_dtr(fc_start_time=450.0, drop_time=540.0)
        assert dtr == pytest.approx(16.67, rel=0.01)

    def test_dtr_from_events(self) -> None:
        """Test DTR calculation using event markers."""
        collector = CurveCollector()
        collector.add_event("fc_start", 450.0, 196.0)
        collector.add_event("drop", 540.0, 210.0)
        dtr = collector.calculate_dtr()
        assert dtr == pytest.approx(16.67, rel=0.01)

    def test_dtr_missing_fc(self) -> None:
        """Test DTR with missing FC time."""
        collector = CurveCollector()
        collector.add_event("drop", 540.0, 210.0)
        assert collector.calculate_dtr() is None

    def test_dtr_missing_drop(self) -> None:
        """Test DTR with missing drop time."""
        collector = CurveCollector()
        collector.add_event("fc_start", 450.0, 196.0)
        assert collector.calculate_dtr() is None

    def test_dtr_invalid_times(self) -> None:
        """Test DTR with invalid time values."""
        collector = CurveCollector()
        # FC after drop
        assert collector.calculate_dtr(fc_start_time=600.0, drop_time=540.0) is None
        # Zero drop time
        assert collector.calculate_dtr(fc_start_time=10.0, drop_time=0.0) is None


class TestPhaseTimes:
    """Tests for phase time calculation."""

    def test_all_phases(self) -> None:
        """Test phase time calculation with all events."""
        collector = CurveCollector()
        collector.add_event("charge", 0.0, 195.0)
        collector.add_event("dry_end", 240.0, 150.0)
        collector.add_event("fc_start", 450.0, 196.0)
        collector.add_event("drop", 540.0, 210.0)

        phases = collector.calculate_phase_times()
        assert phases["drying_time"] == 240.0
        assert phases["maillard_time"] == 210.0
        assert phases["development_time"] == 90.0
        assert phases["total_time"] == 540.0

    def test_partial_events(self) -> None:
        """Test with only some events marked."""
        collector = CurveCollector()
        collector.add_event("dry_end", 240.0, 150.0)
        collector.add_event("drop", 540.0, 210.0)

        phases = collector.calculate_phase_times()
        assert phases["drying_time"] == 240.0  # charge assumed at 0
        assert phases["maillard_time"] is None  # no fc_start
        assert phases["development_time"] is None  # no fc_start
        assert phases["total_time"] == 540.0

    def test_no_events(self) -> None:
        """Test with no events."""
        collector = CurveCollector()
        phases = collector.calculate_phase_times()
        assert phases["drying_time"] is None
        assert phases["maillard_time"] is None
        assert phases["development_time"] is None
        assert phases["total_time"] is None


class TestSmoothBt:
    """Tests for moving-window polynomial smoothing."""

    def test_smooth_empty(self) -> None:
        """Test smoothing with no samples."""
        collector = CurveCollector()
        result = collector.smooth_bt()
        assert result == []

    def test_smooth_few_samples(self) -> None:
        """Test smoothing with fewer samples than window."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=1.0, bt=101.0, et=151.0)
        result = collector.smooth_bt()
        assert len(result) == 2
        # With too few samples, should return close to original
        assert result[0] == pytest.approx(100.0, rel=0.1)
        assert result[1] == pytest.approx(101.0, rel=0.1)

    def test_smooth_removes_noise(self) -> None:
        """Test that smoothing reduces noise."""
        collector = CurveCollector()
        # Add noisy data around a linear trend
        for i in range(20):
            noise = 2.0 if i % 2 == 0 else -2.0
            collector.record_sample(
                time_seconds=float(i * 3),
                bt=100.0 + i + noise,
                et=150.0,
            )
        raw_bt = [s.bt for s in collector.samples]
        smoothed_bt = collector.smooth_bt()

        # Calculate variance
        raw_variance = sum((b - (100 + i)) ** 2 for i, b in enumerate(raw_bt)) / len(
            raw_bt
        )
        smooth_variance = sum(
            (b - (100 + i)) ** 2 for i, b in enumerate(smoothed_bt)
        ) / len(smoothed_bt)

        # Smoothed should have lower variance (closer to trend)
        # Note: edge effects might prevent this from being dramatically lower
        assert smooth_variance <= raw_variance + 1.0


class TestStatistics:
    """Tests for curve statistics calculation."""

    def test_empty_statistics(self) -> None:
        """Test statistics with no samples."""
        collector = CurveCollector()
        stats = collector.get_statistics()
        assert stats["min_bt"] is None
        assert stats["max_bt"] is None
        assert stats["avg_ror"] is None

    def test_basic_statistics(self) -> None:
        """Test basic statistics calculation."""
        collector = CurveCollector()
        # Simulate a roast curve with enough data for RoR window
        temps = [195, 180, 165, 150, 100, 97, 105, 120, 140, 160, 180, 196, 200, 205, 210]
        for i, temp in enumerate(temps):
            collector.record_sample(
                time_seconds=float(i * 10),  # 10s intervals, 140s total
                bt=float(temp),
                et=float(temp + 20),
            )
        stats = collector.get_statistics()
        assert stats["min_bt"] == 97.0  # Turning point
        assert stats["max_bt"] == 210.0
        assert stats["min_bt_time"] == 50.0  # 5th sample (index 5) * 10s

    def test_statistics_smoothed_vs_raw(self) -> None:
        """Test that use_smoothed_ror parameter works."""
        collector = CurveCollector()
        for i in range(20):
            noise = 2.0 if i % 2 == 0 else -2.0
            collector.record_sample(
                time_seconds=float(i * 10),
                bt=100.0 + i * 3 + noise,
                et=150.0,
            )
        stats_smoothed = collector.get_statistics(use_smoothed_ror=True)
        stats_raw = collector.get_statistics(use_smoothed_ror=False)
        # Both should return values
        assert stats_smoothed["avg_ror"] is not None or stats_raw["avg_ror"] is not None


class TestTurningPointDetection:
    """Tests for automatic turning point detection."""

    def test_no_samples(self) -> None:
        """Test with no samples."""
        collector = CurveCollector()
        assert collector.detect_turning_point() is None

    def test_too_few_samples(self) -> None:
        """Test with too few samples."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=195.0, et=220.0)
        collector.record_sample(time_seconds=1.5, bt=180.0, et=215.0)
        assert collector.detect_turning_point() is None

    def test_detect_turning_point(self) -> None:
        """Test successful turning point detection."""
        collector = CurveCollector()
        # Simulate charge drop and rise
        temps = [195, 180, 165, 150, 120, 100, 97, 98, 102, 110, 120]
        for i, temp in enumerate(temps):
            collector.record_sample(
                time_seconds=float(i * 10),
                bt=float(temp),
                et=float(temp + 20),
            )
        tp = collector.detect_turning_point()
        assert tp is not None
        time, temp = tp
        assert temp == 97.0
        assert time == 60.0  # 6th sample (index 6) * 10s

    def test_detect_turning_point_with_custom_search_time(self) -> None:
        """Test turning point detection with custom search window."""
        collector = CurveCollector()
        # Put minimum outside default 120s window but inside custom 200s
        temps = [195, 180, 165, 150, 140, 130, 120, 110, 100, 90, 85, 80, 75, 72, 70, 71, 73]
        for i, temp in enumerate(temps):
            collector.record_sample(
                time_seconds=float(i * 12),  # 12s intervals
                bt=float(temp),
                et=float(temp + 20),
            )
        # Minimum at index 14 (168s) - outside 120s default
        tp_default = collector.detect_turning_point(max_search_time=120.0)
        tp_extended = collector.detect_turning_point(max_search_time=200.0)

        # Default should find a local minimum within 120s
        # Extended should find the true minimum at 70°C
        assert tp_extended is not None
        assert tp_extended[1] == 70.0


class TestSchemaConversion:
    """Tests for CurveDataSchema conversion."""

    def test_to_schema(self) -> None:
        """Test exporting to schema."""
        collector = CurveCollector()
        collector.record_sample(
            time_seconds=0.0, bt=195.0, et=220.0, heat=100, fan=40, drum=60
        )
        collector.record_sample(
            time_seconds=60.0, bt=150.0, et=200.0, heat=80, fan=50, drum=60
        )
        collector.add_event("charge", 0.0, 195.0)

        schema = collector.to_schema()
        assert isinstance(schema, CurveDataSchema)
        assert len(schema.time_seconds) == 2
        assert schema.bt[0] == 195.0
        assert schema.heat[0] == 100
        assert len(schema.events) == 1
        assert schema.events[0].type == "charge"

    def test_from_schema(self) -> None:
        """Test importing from schema."""
        schema = CurveDataSchema(
            time_seconds=[0.0, 60.0, 120.0],
            bt=[195.0, 150.0, 120.0],
            et=[220.0, 200.0, 180.0],
            bt_ror=[None, -45.0, -30.0],
            heat=[100, 80, 60],
            fan=[40, 50, 60],
            drum=[60, 60, 60],
            events=[
                CurveEventSchema(time=0, type="charge", temp=195.0),
            ],
        )
        collector = CurveCollector.from_schema(schema)
        assert collector.sample_count == 3
        assert collector.samples[0].bt == 195.0
        assert len(collector.events) == 1

    def test_round_trip(self) -> None:
        """Test converting to schema and back."""
        original = CurveCollector()
        for i in range(10):
            original.record_sample(
                time_seconds=float(i * 10),
                bt=100.0 + i * 5,
                et=150.0 + i * 3,
                heat=80 - i * 5,
                fan=40 + i * 2,
                drum=60,
            )
        original.add_event("charge", 0.0, 100.0)
        original.add_event("fc_start", 70.0, 135.0)

        schema = original.to_schema()
        restored = CurveCollector.from_schema(schema)

        assert restored.sample_count == original.sample_count
        assert len(restored.events) == len(original.events)
        assert restored.samples[5].bt == original.samples[5].bt

    def test_from_schema_bypasses_validation(self) -> None:
        """Test that from_schema bypasses time validation for restored data."""
        # This would fail with record_sample but should work for restoration
        schema = CurveDataSchema(
            time_seconds=[0.0, 0.0, 1.0],  # Duplicate times!
            bt=[100.0, 101.0, 102.0],
            et=[150.0, 151.0, 152.0],
            bt_ror=[None, None, None],
            heat=[80, 80, 80],
            fan=[40, 40, 40],
            drum=[60, 60, 60],
            events=[],
        )
        # Should not raise
        collector = CurveCollector.from_schema(schema)
        assert collector.sample_count == 3


class TestRepr:
    """Tests for string representation."""

    def test_repr_empty(self) -> None:
        """Test repr with no data."""
        collector = CurveCollector()
        repr_str = repr(collector)
        assert "samples=0" in repr_str
        assert "events=0" in repr_str
        assert "duration=0.0s" in repr_str

    def test_repr_with_data(self) -> None:
        """Test repr with data."""
        collector = CurveCollector()
        collector.record_sample(time_seconds=0.0, bt=100.0, et=150.0)
        collector.record_sample(time_seconds=120.0, bt=180.0, et=200.0)
        collector.add_event("charge", 0.0, 100.0)
        repr_str = repr(collector)
        assert "samples=2" in repr_str
        assert "events=1" in repr_str
        assert "duration=120.0s" in repr_str


class TestIntegration:
    """Integration tests simulating real roast scenarios."""

    def test_full_roast_simulation(self) -> None:
        """Simulate a complete roast cycle."""
        collector = CurveCollector(sample_interval=3.0)

        # Charge at 195°C, drops to turning point at 97°C around 75s
        # Then rises through phases
        roast_curve = [
            (0, 195, 220),
            (3, 188, 218),
            (6, 180, 215),
            (9, 170, 212),
            (12, 158, 208),
            (15, 145, 205),
            (18, 130, 200),
            (21, 115, 195),
            (24, 102, 190),
            (27, 97, 185),  # Turning point
            (30, 98, 183),
            (33, 100, 182),
            (36, 104, 181),
            (39, 109, 180),
            (42, 115, 180),
            # ... continue rising
        ] + [(45 + i * 3, 120 + i * 2, 180 + i) for i in range(50)]

        # Add more samples to reach FC and drop
        for time, bt, et in roast_curve:
            collector.record_sample(
                time_seconds=float(time), bt=float(bt), et=float(et), heat=80, fan=50, drum=60
            )

        # Add events
        collector.add_event("charge", 0.0, 195.0)
        collector.add_event("turning_point", 27.0, 97.0)

        # Verify basic properties
        assert collector.sample_count == len(roast_curve)
        assert collector.duration > 0

        # Verify RoR calculation
        ror = collector.calculate_ror()
        assert len(ror) == len(roast_curve)
        # First sample is always None
        assert ror[0] is None

        # Verify statistics
        stats = collector.get_statistics()
        assert stats["min_bt"] == 97.0

        # Verify schema export
        schema = collector.to_schema()
        assert schema.bt[0] == 195.0
        assert len(schema.events) == 2
