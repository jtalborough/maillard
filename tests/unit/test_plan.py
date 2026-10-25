"""Tests for roast plans (planned curve, targets, validation, snapshot)."""

from __future__ import annotations

from maillard.ai.generate import compute_heuristic_profile
from maillard.db.models import Profile
from maillard.roasting.plan import (
    build_plan_from_profile,
    derive_targets,
    interpolate_planned_curve,
    planned_bt_at,
    validate_plan,
)


def _phases():
    # Fixed waypoints test interpolation independently of machine calibration.
    return [
        {"name": name, "target_time_seconds": t, "target_temp": temp}
        for name, t, temp in [
            ("charge", 0, 220),
            ("turning_point", 95, 95),
            ("dry_end", 300, 160),
            ("maillard", 420, 177),
            ("fc_start", 570, 196),
            ("development", 640, 203),
            ("drop", 712, 210),
        ]
    ]


def test_planned_bt_interpolates_between_waypoints():
    phases = _phases()
    # charge 220 @0 -> turning point 95 @95: halfway is 157.5
    assert planned_bt_at(phases, 47.5) == 157.5
    assert planned_bt_at(phases, 0) == 220
    assert planned_bt_at(phases, 712) == 210
    assert planned_bt_at(phases, 800) is None


def test_curve_sampled_every_5s_to_drop():
    curve = interpolate_planned_curve(_phases())
    assert curve["time_seconds"][0] == 0
    assert curve["time_seconds"][-1] == 712
    assert curve["time_seconds"][1] - curve["time_seconds"][0] == 5
    assert len(curve["time_seconds"]) == len(curve["bt"])
    assert curve["bt"][-1] == 210


def test_derive_targets():
    t = derive_targets(_phases())
    assert t["target_fc_seconds"] == 570
    assert t["target_total_seconds"] == 712
    assert t["target_drop_temp"] == 210
    assert t["phase_dtr"] == 19.9


def test_full_m10_plan_warns_about_extrapolation():
    gp = compute_heuristic_profile(None, batch_size_grams=1200)
    warnings = validate_plan(
        gp.phases, charge_temp=gp.charge_temp, batch_size_grams=1200, target_dtr=gp.target_dtr
    )
    assert any("extrapolated" in warning for warning in warnings)


def test_validate_rejects_drop_that_conflicts_with_level():
    warnings = validate_plan(
        _phases(),
        charge_temp=200,
        batch_size_grams=450,
        target_dtr=20,
        roast_level="Light",
    )
    assert any("rejected" in warning for warning in warnings)


def test_validate_flags_bad_plans():
    phases = _phases()
    phases[6]["target_temp"] = 228
    phases[3]["target_time_seconds"] = 100  # not increasing
    w = validate_plan(phases, charge_temp=260, batch_size_grams=1500, target_dtr=40)
    text = " ".join(w)
    assert "not strictly increasing" in text
    assert "1500 g" in text
    assert "Charge temp" in text
    assert "safety limit" in text
    assert "DTR" in text


async def test_build_plan_snapshot_from_ai_profile(mongo_db):  # noqa: ARG001
    profile = Profile(
        name="Uraga ML",
        phases=_phases(),
        charge_temp=218,
        target_dtr=20,
        batch_size_grams=1000,
        ai_rationale="because",
        plan_source="ai",
        ai_model="claude-sonnet-4-5",
        roast_level="Medium-Light",
    )
    plan = build_plan_from_profile(profile, batch_size_grams=1200, roast_id="r1")
    assert plan.source == "ai"
    assert plan.ai_model == "claude-sonnet-4-5"
    assert plan.batch_size_grams == 1200  # charged batch wins over profile batch
    assert plan.roast_id == "r1"
    assert plan.locked_at is not None
    assert plan.target_fc_seconds == 570
    assert plan.planned_curve["bt"][0] == 220
    # snapshot is a copy: editing the profile later does not change the plan
    profile.phases[0]["target_temp"] = 100
    assert plan.phases[0]["target_temp"] == 220
