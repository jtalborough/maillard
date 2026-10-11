"""Tests for the roast history formatting module."""

from unittest.mock import MagicMock

import pytest

from maillard.ai.history import (
    PastRoastSummary,
    RoastHistory,
    _build_summary,
    _determine_best,
    _fmt_time,
    format_coffee_characteristics,
    format_comparison_data,
    format_history_for_prompt,
)


def _mock_cupping(**kwargs):
    """Create a mock Cupping ORM object."""
    defaults = {
        "aroma": None,
        "acidity": None,
        "sweetness": None,
        "body": None,
        "aftertaste": None,
        "overall": None,
        "flavor_notes": None,
        "defects": None,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _mock_roast(**kwargs):
    """Create a mock Roast ORM object."""
    defaults = {
        "id": "roast-1",
        "start_time": "2025-01-15T10:00:00",
        "charge_temp": 195.0,
        "turning_point_time": 75,
        "turning_point_temp": 97.0,
        "dry_end_time": 240,
        "dry_end_temp": 155.0,
        "fc_start_time": 480,
        "fc_start_temp": 196.0,
        "drop_temp": 210.0,
        "drop_time": 600,
        "dtr": 20.0,
        "total_time_seconds": 600,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


# =============================================================================
# _fmt_time
# =============================================================================


class TestFmtTime:
    def test_none(self):
        assert _fmt_time(None) == "--:--"

    def test_zero(self):
        assert _fmt_time(0) == "0:00"

    def test_sixty_seconds(self):
        assert _fmt_time(60) == "1:00"

    def test_ninety_seconds(self):
        assert _fmt_time(90) == "1:30"

    def test_ten_minutes(self):
        assert _fmt_time(600) == "10:00"

    def test_single_digit_seconds(self):
        assert _fmt_time(65) == "1:05"


# =============================================================================
# _build_summary (uses aggregate_cuppings internally)
# =============================================================================


class TestBuildSummary:
    def test_maps_all_fields(self):
        cupping = _mock_cupping(
            aroma=8.0, acidity=7.5, sweetness=8.5, body=7.0, aftertaste=7.5, overall=8.0,
            flavor_notes=(["citrus"]),
        )
        roast = _mock_roast()
        summary = _build_summary(roast, cuppings=[cupping])

        assert summary.roast_id == "roast-1"
        assert summary.date == "2025-01-15"
        assert summary.charge_temp == 195.0
        assert summary.turning_point_time == 75
        assert summary.turning_point_temp == 97.0
        assert summary.dry_end_time == 240
        assert summary.dry_end_temp == 155.0
        assert summary.fc_start_time == 480
        assert summary.fc_start_temp == 196.0
        assert summary.drop_temp == 210.0
        assert summary.drop_time == 600
        assert summary.dtr == 20.0
        assert summary.total_time_seconds == 600
        assert summary.cupping_avg_total == 46.5
        assert summary.cupping_count == 1
        assert summary.ranked_flavor_notes == ["citrus"]

    def test_no_start_time(self):
        roast = _mock_roast(start_time=None)
        summary = _build_summary(roast)
        assert summary.date == ""

    def test_no_cuppings(self):
        roast = _mock_roast()
        summary = _build_summary(roast, cuppings=[])
        assert summary.cupping_avg_total is None
        assert summary.cupping_count == 0
        assert summary.ranked_flavor_notes == []

    def test_multiple_cuppings_averaged(self):
        c1 = _mock_cupping(aroma=8.0, acidity=7.0, sweetness=8.0, body=7.0, aftertaste=7.0, overall=7.5)  # total=44.5
        c2 = _mock_cupping(aroma=9.0, acidity=8.0, sweetness=8.5, body=7.5, aftertaste=8.0, overall=8.5)  # total=49.5
        roast = _mock_roast()
        summary = _build_summary(roast, cuppings=[c1, c2])
        assert summary.cupping_count == 2
        assert summary.cupping_avg_total == 47.0  # (44.5+49.5)/2

    def test_flavor_notes_frequency_ranked(self):
        c1 = _mock_cupping(aroma=8.0, flavor_notes=(["citrus", "floral"]))
        c2 = _mock_cupping(aroma=8.0, flavor_notes=(["citrus", "honey"]))
        roast = _mock_roast()
        summary = _build_summary(roast, cuppings=[c1, c2])
        # citrus: 2, floral: 1, honey: 1
        assert summary.ranked_flavor_notes[0] == "citrus"
        assert set(summary.ranked_flavor_notes[1:]) == {"floral", "honey"}


# =============================================================================
# _determine_best
# =============================================================================


class TestDetermineBest:
    def test_empty_list(self):
        assert _determine_best([]) is None

    def test_all_without_cupping(self):
        s1 = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=None,
        )
        s2 = PastRoastSummary(
            roast_id="r2", date="2025-01-16", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=None,
        )
        # No cupping data -> returns first (most recent)
        result = _determine_best([s1, s2])
        assert result is not None
        assert result.roast_id == "r1"

    def test_mixed_scored_unscored(self):
        unscored = PastRoastSummary(
            roast_id="r1", date="2025-01-16", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=None,
        )
        scored = PastRoastSummary(
            roast_id="r2", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=45.0,
        )
        result = _determine_best([unscored, scored])
        assert result is not None
        assert result.roast_id == "r2"

    def test_tie_break_by_date(self):
        s1 = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=45.0,
        )
        s2 = PastRoastSummary(
            roast_id="r2", date="2025-01-16", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=45.0,
        )
        # Same cupping_avg_total -> most recent date wins
        result = _determine_best([s1, s2])
        assert result is not None
        assert result.roast_id == "r2"

    def test_highest_cupping_wins(self):
        s1 = PastRoastSummary(
            roast_id="r1", date="2025-01-16", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=40.0,
        )
        s2 = PastRoastSummary(
            roast_id="r2", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=48.0,
        )
        result = _determine_best([s1, s2])
        assert result is not None
        assert result.roast_id == "r2"


# =============================================================================
# format_history_for_prompt
# =============================================================================


class TestFormatHistoryForPrompt:
    def test_none_history(self):
        assert format_history_for_prompt(None) is None

    def test_empty_past_roasts(self):
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[],
        )
        assert format_history_for_prompt(history) is None

    def test_with_best_roast(self):
        best = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=46.0,
            ranked_flavor_notes=["citrus", "floral"],
        )
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[best], best_roast=best,
        )
        result = format_history_for_prompt(history)
        assert result is not None
        assert "BEST: 2025-01-15" in result
        assert "cupping 46/60 avg" in result
        assert "citrus, floral" in result
        assert "Compare your current pace" in result

    def test_with_multiple_cuppings_shows_count(self):
        best = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=46.0,
            cupping_count=3,
            ranked_flavor_notes=["citrus"],
        )
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[best], best_roast=best,
        )
        result = format_history_for_prompt(history)
        assert result is not None
        assert "(n=3)" in result

    def test_without_cupping_data(self):
        """When best_roast has no cupping_avg_total, BEST line is skipped."""
        summary = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=None,
        )
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[summary], best_roast=summary,
        )
        result = format_history_for_prompt(history)
        assert result is not None
        assert "BEST:" not in result
        assert "## Past Roasts" in result

    def test_multiple_roasts_best_shown_separately(self):
        best = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=46.0,
            ranked_flavor_notes=["citrus"],
        )
        other = PastRoastSummary(
            roast_id="r2", date="2025-01-14", charge_temp=190.0,
            turning_point_time=80, turning_point_temp=95.0,
            dry_end_time=250, dry_end_temp=153.0,
            fc_start_time=490, fc_start_temp=195.0,
            drop_temp=208.0, drop_time=590, dtr=18.0,
            total_time_seconds=590, cupping_avg_total=None,
        )
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[best, other], best_roast=best,
        )
        result = format_history_for_prompt(history)
        assert result is not None
        lines = result.split("\n")
        # BEST is shown separately and r2 is listed
        assert any("BEST:" in line for line in lines)
        assert any("2025-01-14" in line for line in lines)


# =============================================================================
# format_comparison_data
# =============================================================================


class TestFormatComparisonData:
    def test_no_history(self):
        metrics = {"drop_temp": 210.0, "total_time_seconds": 600, "dtr": 20.0}
        result = format_comparison_data(metrics, None)
        assert "Post-Roast Comparison" in result
        assert "drop temp: 210C" in result.lower() or "Drop temp: 210C" in result
        assert "first roast of this coffee" in result.lower()

    def test_with_history_and_best(self):
        best = PastRoastSummary(
            roast_id="r1", date="2025-01-15", charge_temp=195.0,
            turning_point_time=75, turning_point_temp=97.0,
            dry_end_time=240, dry_end_temp=155.0,
            fc_start_time=480, fc_start_temp=196.0,
            drop_temp=210.0, drop_time=600, dtr=20.0,
            total_time_seconds=600, cupping_avg_total=46.0,
            ranked_flavor_notes=["citrus"],
        )
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[best], best_roast=best,
        )
        metrics = {"drop_temp": 212.0, "total_time_seconds": 610, "dtr": 21.5, "fc_start_time": 490}
        result = format_comparison_data(metrics, history)
        assert "This Roast" in result
        assert "Past Roasts" in result
        assert "Best (2025-01-15)" in result
        assert "citrus" in result
        assert "avg" in result

    def test_first_roast_no_past(self):
        history = RoastHistory(
            coffee_name="Test", coffee_origin="Ethiopia",
            coffee_processing=None, coffee_altitude=None, coffee_density=None,
            past_roasts=[],
        )
        metrics = {"drop_temp": 210.0}
        result = format_comparison_data(metrics, history)
        assert "first roast of this coffee" in result.lower()

    def test_current_metrics_partial(self):
        metrics = {"dtr": 22.0}  # only DTR
        result = format_comparison_data(metrics, None)
        assert "DTR: 22.0%" in result
        assert "Drop temp" not in result


# =============================================================================
# format_coffee_characteristics
# =============================================================================


class TestFormatCoffeeCharacteristics:
    def test_all_fields(self):
        history = RoastHistory(
            coffee_name="Ethiopia Yirgacheffe",
            coffee_origin="Ethiopia",
            coffee_processing="Washed",
            coffee_altitude=1900,
            coffee_density="High",
        )
        result = format_coffee_characteristics(history)
        assert "Ethiopia Yirgacheffe" in result
        assert "Ethiopia" in result
        assert "Washed" in result
        assert "1900 MASL" in result
        assert "High" in result

    def test_minimal_fields(self):
        history = RoastHistory(
            coffee_name="Test Coffee",
            coffee_origin="Brazil",
            coffee_processing=None,
            coffee_altitude=None,
            coffee_density=None,
        )
        result = format_coffee_characteristics(history)
        assert "Test Coffee" in result
        assert "Brazil" in result
        assert "Processing" not in result
        assert "Altitude" not in result
        assert "Density" not in result


@pytest.mark.parametrize("dtr", [None, 0.0, 20.0])
def test_comparison_missing_drop_temp(dtr):
    best = _build_summary(_mock_roast(drop_temp=None, dtr=dtr), [])
    history = RoastHistory(
        coffee_name="Test", coffee_origin=None, coffee_processing=None,
        coffee_altitude=None, coffee_density=None, past_roasts=[best], best_roast=best,
    )
    result = format_comparison_data({}, history)
    assert "Best (" in result
    assert "drop" not in result
    if dtr is not None:
        assert f"DTR {dtr:.0f}%" in result
