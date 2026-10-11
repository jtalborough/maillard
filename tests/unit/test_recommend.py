"""Tests for pre-roast AI recommendation module."""

import pytest

from maillard.ai.history import PastRoastSummary, RoastHistory
from maillard.ai.recommend import (
    DEFAULT_CHARGE_TEMP,
    DEFAULT_DRUM,
    DEFAULT_FAN,
    DEFAULT_HEAT,
    PreRoastRecommendation,
    compute_heuristic_recommendation,
)


def _summary(
    roast_id: str = "r1",
    date: str = "2025-01-15",
    charge_temp: float | None = 195.0,
    cupping_avg_total: float | None = None,
    **kwargs,
) -> PastRoastSummary:
    """Create a PastRoastSummary with sensible defaults."""
    defaults = dict(
        turning_point_time=75,
        turning_point_temp=97.0,
        dry_end_time=240,
        dry_end_temp=155.0,
        fc_start_time=480,
        fc_start_temp=196.0,
        drop_temp=210.0,
        drop_time=600,
        dtr=20.0,
        total_time_seconds=600,
        ranked_flavor_notes=[],
    )
    defaults.update(kwargs)
    return PastRoastSummary(
        roast_id=roast_id,
        date=date,
        charge_temp=charge_temp,
        cupping_avg_total=cupping_avg_total,
        **defaults,
    )


def _history(
    past_roasts: list[PastRoastSummary] | None = None,
    best_roast: PastRoastSummary | None = None,
    coffee_name: str = "Ethiopia Yirgacheffe",
    coffee_origin: str = "Ethiopia",
    coffee_processing: str | None = None,
    coffee_altitude: int | None = None,
    coffee_density: str | None = None,
) -> RoastHistory:
    """Create a RoastHistory with sensible defaults."""
    return RoastHistory(
        coffee_name=coffee_name,
        coffee_origin=coffee_origin,
        coffee_processing=coffee_processing,
        coffee_altitude=coffee_altitude,
        coffee_density=coffee_density,
        past_roasts=past_roasts or [],
        best_roast=best_roast,
    )


# =============================================================================
# compute_heuristic_recommendation
# =============================================================================


class TestHistoryWithCupping:
    """Priority 1: Best past roast with cupping data."""

    def test_uses_best_roast_charge_temp(self):
        best = _summary(charge_temp=192.0, cupping_avg_total=48.0)
        history = _history(past_roasts=[best], best_roast=best)

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == 192.0
        assert rec.confidence == "high"
        assert rec.source == "heuristic"
        assert "best past roast" in rec.reasoning.lower()
        assert "48/60" in rec.reasoning

    def test_high_confidence_with_cupping(self):
        best = _summary(charge_temp=198.0, cupping_avg_total=45.0, date="2025-01-20")
        other = _summary(roast_id="r2", charge_temp=190.0, cupping_avg_total=40.0, date="2025-01-15")
        history = _history(past_roasts=[best, other], best_roast=best)

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == 198.0
        assert rec.confidence == "high"

    def test_profile_does_not_override_history_with_cupping(self):
        """Profile charge_temp should NOT override a cupped best roast."""
        best = _summary(charge_temp=192.0, cupping_avg_total=48.0)
        history = _history(past_roasts=[best], best_roast=best)

        rec = compute_heuristic_recommendation(history, profile_charge_temp=205.0)

        assert rec.charge_temp == 192.0
        assert rec.confidence == "high"


class TestHistoryNoCupping:
    """Priority 2: Most recent past roast without cupping data."""

    def test_uses_most_recent_charge_temp(self):
        recent = _summary(charge_temp=197.0, cupping_avg_total=None, date="2025-01-20")
        older = _summary(roast_id="r2", charge_temp=190.0, cupping_avg_total=None, date="2025-01-10")
        history = _history(past_roasts=[recent, older], best_roast=recent)

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == 197.0
        assert rec.confidence == "medium"
        assert "most recent" in rec.reasoning.lower()

    def test_medium_confidence_no_cupping(self):
        recent = _summary(charge_temp=200.0, cupping_avg_total=None)
        history = _history(past_roasts=[recent], best_roast=recent)

        rec = compute_heuristic_recommendation(history)

        assert rec.confidence == "medium"


class TestProfileOnly:
    """Priority 3: Profile charge temp with no history."""

    def test_uses_profile_charge_temp(self):
        rec = compute_heuristic_recommendation(None, profile_charge_temp=210.0)

        assert rec.charge_temp == 210.0
        assert rec.confidence == "medium"
        assert "profile" in rec.reasoning.lower()

    def test_uses_profile_when_history_has_no_charge_data(self):
        """History exists but best roast has no charge_temp -> fall through to profile."""
        best = _summary(charge_temp=None, cupping_avg_total=None)
        history = _history(past_roasts=[best], best_roast=best)

        rec = compute_heuristic_recommendation(history, profile_charge_temp=205.0)

        assert rec.charge_temp == 205.0
        assert rec.confidence == "medium"

    def test_profile_with_no_history_object(self):
        """history=None but profile_charge_temp provided."""
        rec = compute_heuristic_recommendation(None, profile_charge_temp=195.0)

        assert rec.charge_temp == 195.0
        assert rec.confidence == "medium"


class TestCoffeeCharacteristics:
    """Priority 4: Coffee characteristics adjust from 200C base."""

    def test_high_density_adds_5(self):
        history = _history(coffee_density="High")

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP + 5
        assert rec.confidence == "medium"
        assert "high density" in rec.reasoning.lower()

    def test_low_density_subtracts_5(self):
        history = _history(coffee_density="Low")

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP - 5

    def test_natural_process_subtracts_5(self):
        history = _history(coffee_processing="Natural")

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP - 5
        assert "natural" in rec.reasoning.lower()

    def test_honey_process_subtracts_3(self):
        history = _history(coffee_processing="Honey")

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP - 3

    def test_high_altitude_adds_5(self):
        history = _history(coffee_altitude=2000)

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP + 5
        assert "altitude" in rec.reasoning.lower()

    def test_altitude_at_threshold_no_adjustment(self):
        """Altitude exactly at 1800m should NOT trigger adjustment."""
        history = _history(coffee_altitude=1800)

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP

    def test_combined_adjustments(self):
        """High density (+5) + Natural (-5) + High altitude (+5) = +5."""
        history = _history(
            coffee_density="High",
            coffee_processing="Natural",
            coffee_altitude=2000,
        )

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP + 5
        assert rec.confidence == "medium"

    def test_washed_no_processing_adjustment(self):
        history = _history(coffee_processing="Washed")

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP

    def test_no_characteristics_low_confidence(self):
        """History object exists but no useful characteristics."""
        history = _history(
            coffee_processing=None,
            coffee_altitude=None,
            coffee_density=None,
        )

        rec = compute_heuristic_recommendation(history)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP
        assert rec.confidence == "low"


class TestMinimalData:
    """Priority 5: Bare default (no coffee, no profile)."""

    def test_default_charge_temp(self):
        rec = compute_heuristic_recommendation(None)

        assert rec.charge_temp == DEFAULT_CHARGE_TEMP
        assert rec.confidence == "low"
        assert rec.source == "heuristic"

    def test_default_controls(self):
        rec = compute_heuristic_recommendation(None)

        assert rec.heat_percent == DEFAULT_HEAT
        assert rec.fan_percent == DEFAULT_FAN
        assert rec.drum_percent == DEFAULT_DRUM


class TestControlDefaults:
    """Heat/fan/drum should always be standard preheat defaults."""

    def test_controls_with_history(self):
        best = _summary(charge_temp=195.0, cupping_avg_total=48.0)
        history = _history(past_roasts=[best], best_roast=best)

        rec = compute_heuristic_recommendation(history)

        assert rec.heat_percent == DEFAULT_HEAT
        assert rec.fan_percent == DEFAULT_FAN
        assert rec.drum_percent == DEFAULT_DRUM

    def test_controls_with_profile(self):
        rec = compute_heuristic_recommendation(None, profile_charge_temp=200.0)

        assert rec.heat_percent == DEFAULT_HEAT
        assert rec.fan_percent == DEFAULT_FAN
        assert rec.drum_percent == DEFAULT_DRUM


class TestDataclass:
    """PreRoastRecommendation dataclass fields."""

    def test_fields(self):
        rec = PreRoastRecommendation(
            charge_temp=200.0,
            heat_percent=100,
            fan_percent=30,
            drum_percent=50,
            reasoning="test",
            source="heuristic",
            confidence="high",
        )
        assert rec.charge_temp == 200.0
        assert rec.heat_percent == 100
        assert rec.fan_percent == 30
        assert rec.drum_percent == 50
        assert rec.reasoning == "test"
        assert rec.source == "heuristic"
        assert rec.confidence == "high"
