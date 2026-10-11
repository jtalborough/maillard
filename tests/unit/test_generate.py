"""Tests for AI profile generation module."""

import pytest

from maillard.ai.calibration import get_calibration
from maillard.ai.generate import (
    PHASE_NAMES,
    GeneratedProfile,
    _validate_ai_phases,
    compute_heuristic_profile,
)
from maillard.ai.history import RoastHistory


def _history(
    coffee_name: str = "Ethiopia Yirgacheffe",
    coffee_origin: str = "Ethiopia",
    coffee_processing: str | None = None,
    coffee_altitude: int | None = None,
    coffee_density: str | None = None,
    coffee_moisture: float | None = None,
) -> RoastHistory:
    """Create a RoastHistory with sensible defaults for testing."""
    return RoastHistory(
        coffee_name=coffee_name,
        coffee_origin=coffee_origin,
        coffee_processing=coffee_processing,
        coffee_altitude=coffee_altitude,
        coffee_density=coffee_density,
        coffee_moisture=coffee_moisture,
        past_roasts=[],
        best_roast=None,
    )


# =============================================================================
# compute_heuristic_profile — basic structure
# =============================================================================


class TestHeuristicBasicStructure:
    """Verify the heuristic profile has correct structure."""

    def test_returns_generated_profile(self):
        result = compute_heuristic_profile(None)
        assert isinstance(result, GeneratedProfile)

    def test_has_7_phases(self):
        result = compute_heuristic_profile(None)
        assert len(result.phases) == 7

    def test_phase_names_correct(self):
        result = compute_heuristic_profile(None)
        names = [p["name"] for p in result.phases]
        assert names == PHASE_NAMES

    def test_times_monotonically_increasing(self):
        result = compute_heuristic_profile(None)
        times = [p["target_time_seconds"] for p in result.phases]
        for i in range(1, len(times)):
            assert times[i] > times[i - 1]

    def test_source_is_heuristic(self):
        result = compute_heuristic_profile(None)
        assert result.source == "heuristic"

    def test_default_charge_temp(self):
        result = compute_heuristic_profile(None)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"]

    def test_default_batch_is_full_m10_batch(self):
        result = compute_heuristic_profile(None)
        assert result.batch_size_grams == 1200
        assert "1200g" in result.name

    def test_smaller_batch_lowers_charge_and_shortens(self):
        full = compute_heuristic_profile(None, batch_size_grams=1200)
        small = compute_heuristic_profile(None, batch_size_grams=300)
        assert small.charge_temp == get_calibration(300)["charge_temp_c"]
        assert small.charge_temp < full.charge_temp
        assert small.phases[4]["target_time_seconds"] < full.phases[4]["target_time_seconds"]
        assert small.phases[6]["target_time_seconds"] < full.phases[6]["target_time_seconds"]

    def test_drop_time_matches_dtr_target(self):
        result = compute_heuristic_profile(None, roast_level="Medium", brew_method="Filter")
        fc = result.phases[4]["target_time_seconds"]
        drop = result.phases[6]["target_time_seconds"]
        assert abs((drop - fc) / drop * 100 - result.target_dtr) < 0.5

    def test_default_name_without_history(self):
        result = compute_heuristic_profile(None)
        assert "Custom" in result.name

    def test_name_includes_coffee_name(self):
        history = _history(coffee_name="Kenya AA")
        result = compute_heuristic_profile(history)
        assert "Kenya AA" in result.name


# =============================================================================
# compute_heuristic_profile — roast level adjustments
# =============================================================================


class TestRoastLevelAdjustments:
    """Verify drop temp and timing adjustments by roast level."""

    def test_light_drop_temp(self):
        result = compute_heuristic_profile(None, roast_level="Light")
        drop_phase = result.phases[6]
        assert drop_phase["target_temp"] == get_calibration(result.batch_size_grams)["drop_temps"]["light"]

    def test_medium_drop_temp(self):
        result = compute_heuristic_profile(None, roast_level="Medium")
        drop_phase = result.phases[6]
        assert drop_phase["target_temp"] == get_calibration(result.batch_size_grams)["drop_temps"]["medium"]

    def test_dark_drop_temp(self):
        result = compute_heuristic_profile(None, roast_level="Dark")
        drop_phase = result.phases[6]
        assert drop_phase["target_temp"] == get_calibration(result.batch_size_grams)["drop_temps"]["dark"]

    def test_light_shorter_development(self):
        result = compute_heuristic_profile(None, roast_level="Light")
        drop_time = result.phases[6]["target_time_seconds"]
        fc_time = result.phases[4]["target_time_seconds"]
        assert abs((drop_time - fc_time) / drop_time - 0.18) < 0.001

    def test_dark_longer_development(self):
        result = compute_heuristic_profile(None, roast_level="Dark")
        drop_time = result.phases[6]["target_time_seconds"]
        fc_time = result.phases[4]["target_time_seconds"]
        assert abs((drop_time - fc_time) / drop_time - 0.22) < 0.001

    def test_medium_default_timing(self):
        result = compute_heuristic_profile(None, roast_level="Medium")
        drop_time = result.phases[6]["target_time_seconds"]
        fc_time = result.phases[4]["target_time_seconds"]
        assert abs((drop_time - fc_time) / drop_time - 0.20) < 0.001


# =============================================================================
# compute_heuristic_profile — coffee characteristic adjustments
# =============================================================================


class TestCoffeeCharacteristicAdjustments:
    """Verify charge temp adjustments by coffee characteristics."""

    def test_natural_process_lowers_charge(self):
        history = _history(coffee_processing="Natural")
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"] - 5

    def test_honey_process_lowers_charge(self):
        history = _history(coffee_processing="Honey")
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"] - 3

    def test_washed_no_adjustment(self):
        history = _history(coffee_processing="Washed")
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"]

    def test_high_density_raises_charge(self):
        history = _history(coffee_density="High")
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"] + 5

    def test_low_density_lowers_charge(self):
        history = _history(coffee_density="Low")
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"] - 5

    def test_high_altitude_raises_charge(self):
        history = _history(coffee_altitude=2000)
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"] + 5

    def test_altitude_at_threshold_no_adjustment(self):
        history = _history(coffee_altitude=1800)
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"]

    def test_combined_adjustments(self):
        """Natural (-5) + High density (+5) + High altitude (+5) = +5."""
        history = _history(
            coffee_processing="Natural",
            coffee_density="High",
            coffee_altitude=2000,
        )
        result = compute_heuristic_profile(history)
        assert result.charge_temp == get_calibration(result.batch_size_grams)["charge_temp_c"] + 5

    def test_charge_phase_temp_matches_charge_temp(self):
        history = _history(coffee_processing="Natural")
        result = compute_heuristic_profile(history)
        assert result.phases[0]["target_temp"] == result.charge_temp


# =============================================================================
# compute_heuristic_profile — moisture adjustments
# =============================================================================


class TestMoistureAdjustments:
    """Verify drying phase extension for high moisture."""

    def test_high_moisture_extends_drying(self):
        history = _history(coffee_moisture=13.5)
        result = compute_heuristic_profile(history)
        base = compute_heuristic_profile(None)
        assert result.phases[2]["target_time_seconds"] == base.phases[2]["target_time_seconds"] + 15

    def test_high_moisture_pushes_subsequent_phases(self):
        history = _history(coffee_moisture=13.0)
        default = compute_heuristic_profile(None)
        adjusted = compute_heuristic_profile(history)

        # Phases 3-4 shift by 15 s; development/drop are re-derived from the
        # later first crack, so they shift by at least 15 s.
        for i in range(3, 5):
            assert adjusted.phases[i]["target_time_seconds"] == default.phases[i]["target_time_seconds"] + 15
        for i in range(5, 7):
            assert adjusted.phases[i]["target_time_seconds"] >= default.phases[i]["target_time_seconds"] + 15

    def test_normal_moisture_no_extension(self):
        history = _history(coffee_moisture=11.5)
        result = compute_heuristic_profile(history)
        assert result.phases[2]["target_time_seconds"] == compute_heuristic_profile(None).phases[2]["target_time_seconds"]

    def test_exactly_11_5_no_extension(self):
        history = _history(coffee_moisture=11.5)
        result = compute_heuristic_profile(history)
        assert result.phases[2]["target_time_seconds"] == compute_heuristic_profile(None).phases[2]["target_time_seconds"]

    def test_moisture_rationale_mentioned(self):
        history = _history(coffee_moisture=13.0)
        result = compute_heuristic_profile(history)
        assert "moisture" in result.ai_rationale.lower()


# =============================================================================
# compute_heuristic_profile — brew method / DTR
# =============================================================================


class TestBrewMethodDTR:
    """Verify DTR target by brew method."""

    def test_filter_dtr(self):
        result = compute_heuristic_profile(None, brew_method="Filter")
        assert result.target_dtr == 20.0

    def test_espresso_dtr(self):
        result = compute_heuristic_profile(None, brew_method="Espresso")
        assert result.target_dtr == 23.0

    def test_both_dtr(self):
        result = compute_heuristic_profile(None, brew_method="Both")
        assert result.target_dtr == 21.0


# =============================================================================
# compute_heuristic_profile — rationale
# =============================================================================


class TestRationale:
    """Verify rationale generation."""

    def test_rationale_not_empty(self):
        result = compute_heuristic_profile(None)
        assert result.ai_rationale
        assert len(result.ai_rationale) > 0

    def test_rationale_mentions_roast_level(self):
        result = compute_heuristic_profile(None, roast_level="Dark")
        assert "dark" in result.ai_rationale.lower()

    def test_rationale_mentions_processing(self):
        history = _history(coffee_processing="Natural")
        result = compute_heuristic_profile(history)
        assert "natural" in result.ai_rationale.lower()


# =============================================================================
# _validate_ai_phases
# =============================================================================


class TestValidateAiPhases:
    """Verify AI phase validation logic."""

    def test_valid_phases(self):
        phases = compute_heuristic_profile(None).phases
        assert _validate_ai_phases(phases) is True

    def test_wrong_count(self):
        phases = [{"name": "charge", "target_time_seconds": 0}]
        assert _validate_ai_phases(phases) is False

    def test_wrong_names(self):
        phases = [
            {"name": "wrong", "target_time_seconds": i * 90}
            for i in range(7)
        ]
        assert _validate_ai_phases(phases) is False

    def test_non_monotonic_times(self):
        phases = [
            {"name": n, "target_time_seconds": 100}  # all same time
            for n in PHASE_NAMES
        ]
        assert _validate_ai_phases(phases) is False

    def test_decreasing_times(self):
        phases = [
            {"name": n, "target_time_seconds": (6 - i) * 90}
            for i, n in enumerate(PHASE_NAMES)
        ]
        assert _validate_ai_phases(phases) is False


# =============================================================================
# GeneratedProfile dataclass
# =============================================================================


class TestGeneratedProfile:
    """Verify GeneratedProfile fields."""

    def test_fields(self):
        profile = GeneratedProfile(
            name="Test Profile",
            phases=[],
            charge_temp=200.0,
            target_dtr=20.0,
            ai_rationale="test rationale",
            source="heuristic",
        )
        assert profile.name == "Test Profile"
        assert profile.charge_temp == 200.0
        assert profile.target_dtr == 20.0
        assert profile.source == "heuristic"
        assert profile.adjustments == []

    def test_optional_fields_default(self):
        profile = GeneratedProfile(
            name="Test",
            phases=[],
            charge_temp=200.0,
            target_dtr=20.0,
            ai_rationale="",
            source="heuristic",
        )
        assert profile.roast_level is None
        assert profile.brew_method is None
        assert profile.flavor_goal is None
        assert profile.batch_size_grams is None


async def _generate_payload(payload):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from maillard.ai.generate import generate_ai_profile

    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])
    )))
    baseline = compute_heuristic_profile(None, batch_size_grams=450)
    return await generate_ai_profile(baseline, None, client, "test-model")


def _valid_payload():
    import copy
    baseline = compute_heuristic_profile(None, batch_size_grams=450)
    return {"name": baseline.name, "phases": copy.deepcopy(baseline.phases),
            "charge_temp": baseline.charge_temp, "target_dtr": baseline.target_dtr,
            "rationale": "Calibrated plan"}


@pytest.mark.asyncio
async def test_valid_ai_plan_untouched():
    payload = _valid_payload()
    result = await _generate_payload(payload)
    assert result.source == "ai"
    assert result.phases == payload["phases"]
    assert result.charge_temp == payload["charge_temp"]
    assert result.target_dtr == payload["target_dtr"]
    assert result.warnings == []


@pytest.mark.parametrize("failure", ["fahrenheit", "non_numeric", "times", "missing",
                                    "boolean", "control_range",
                                    "batch_time", "temperature_decrease", "nan"])
@pytest.mark.asyncio
async def test_invalid_ai_plan_falls_back(failure):
    payload = _valid_payload()
    if failure == "fahrenheit":
        for p in payload["phases"]:
            p["target_temp"] = p["target_temp"] * 9 / 5 + 32
    elif failure == "non_numeric":
        payload["phases"][2]["target_temp"] = "hot"
    elif failure == "times":
        payload["phases"][3]["target_time_seconds"] = 1
    elif failure == "missing":
        payload["phases"].pop()
    elif failure == "boolean":
        payload["charge_temp"] = True
    elif failure == "control_range":
        payload["phases"][0]["fan_percent"] = 101
    elif failure == "batch_time":
        for p in payload["phases"]:
            p["target_time_seconds"] *= 2
    elif failure == "nan":
        payload["target_dtr"] = float("nan")
    else:
        payload["phases"][3]["target_temp"] = 100
    result = await _generate_payload(payload)
    assert result.source == "heuristic"
    assert result.phases == compute_heuristic_profile(None, batch_size_grams=450).phases
    assert result.warnings[0].startswith("AI plan rejected:")
    assert result.warnings[0].endswith("; using calibrated heuristic")


@pytest.mark.asyncio
async def test_numeric_strings_coerced():
    payload = _valid_payload()
    for p in payload["phases"]:
        for key in ("target_temp", "target_time_seconds", "heat_percent", "fan_percent", "drum_percent"):
            p[key] = str(p[key])
    payload["charge_temp"] = str(payload["charge_temp"])
    payload["target_dtr"] = str(payload["target_dtr"])
    result = await _generate_payload(payload)
    assert result.source == "ai"
    assert result.phases == _valid_payload()["phases"]
    assert result.warnings == []


@pytest.mark.parametrize("repair", ["charge", "dtr", "drop", "fc"])
@pytest.mark.asyncio
async def test_ai_plan_repairs_warn(repair):
    payload = _valid_payload()
    if repair == "charge":
        payload["charge_temp"] += 10
    elif repair == "dtr":
        payload["target_dtr"] = 30
    elif repair == "drop":
        payload["phases"][6]["target_temp"] = 230
    else:
        payload["phases"][4]["target_temp"] = 194
    result = await _generate_payload(payload)
    assert result.source == "ai"
    assert len(result.warnings) == 1
    if repair == "charge":
        assert result.charge_temp == result.phases[0]["target_temp"]
    elif repair == "dtr":
        assert result.target_dtr == round((1 - result.phases[4]["target_time_seconds"] / result.phases[6]["target_time_seconds"]) * 100, 1)
    elif repair == "drop":
        assert result.phases[6]["target_temp"] == get_calibration(450)["drop_temps"]["medium"] + 8
    else:
        assert result.phases[4]["target_temp"] == get_calibration(450)["fc_start_temp"]


def test_ai_phase_rounds_fractional_integer_targets():
    from maillard.ai.generate import _AIPhase

    phase = _AIPhase.model_validate({
        "name": "dry_end", "target_time_seconds": 300.4, "target_temp": 155,
        "heat_percent": "72.5", "fan_percent": 35.0, "drum_percent": 60,
    })
    assert phase.target_time_seconds == 300
    assert phase.heat_percent == 72
    assert phase.fan_percent == 35
