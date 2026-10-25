"""Roast plans: planned BT curve, derived targets, validation, snapshotting.

A *plan* is the profile a roast is made against, frozen at charge. Maillard
stores it per roast (``RoastPlan``) so the planned curve can be overlaid on
the live chart, compared after the roast, and synced to the site.

Phase targets are treated as BT waypoints; the planned curve is the linear
interpolation between them. That is a deliberately simple model: it is a
guide for a manual roaster, not a control trajectory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maillard.db.models import Profile, RoastPlan

PLANNED_CURVE_STEP_SECONDS = 5

# Plan sanity bounds (Kaleido M10, up to 1.2 kg). Violations are reported as
# warnings; the operator stays in manual control and decides.
MAX_BATCH_GRAMS = 1200
CHARGE_TEMP_RANGE = (150.0, 240.0)
TOTAL_TIME_RANGE = (300, 1200)  # seconds
DTR_RANGE = (10.0, 30.0)


def _sorted_waypoints(phases: list[dict[str, Any]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for ph in phases:
        t = ph.get("target_time_seconds")
        temp = ph.get("target_temp")
        if t is None or temp is None:
            continue
        points.append((float(t), float(temp)))
    points.sort(key=lambda p: p[0])
    return points


def planned_bt_at(phases: list[dict[str, Any]], t: float) -> float | None:
    """Planned BT at ``t`` seconds after charge (None past the drop target)."""
    pts = _sorted_waypoints(phases)
    if not pts or t < pts[0][0] or t > pts[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(pts, pts[1:], strict=False):
        if t0 <= t <= t1:
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return pts[-1][1]


def interpolate_planned_curve(
    phases: list[dict[str, Any]], step: int = PLANNED_CURVE_STEP_SECONDS
) -> dict[str, list[float]]:
    """Planned BT curve sampled every ``step`` seconds from charge to drop."""
    pts = _sorted_waypoints(phases)
    if len(pts) < 2:
        return {"time_seconds": [], "bt": []}
    end = pts[-1][0]
    times: list[float] = []
    t = pts[0][0]
    while t < end:
        times.append(t)
        t += step
    times.append(end)
    bt = [round(planned_bt_at(phases, x) or 0.0, 1) for x in times]
    return {"time_seconds": times, "bt": bt}


def _phase(phases: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((p for p in phases if p.get("name") == name), None)


def derive_targets(phases: list[dict[str, Any]]) -> dict[str, Any]:
    """Targets the operator cares about, derived from the phase table."""
    fc = _phase(phases, "fc_start")
    drop = _phase(phases, "drop")
    fc_t = int(fc["target_time_seconds"]) if fc else None
    drop_t = int(drop["target_time_seconds"]) if drop else None
    dtr = None
    if fc_t is not None and drop_t:
        dtr = round((drop_t - fc_t) / drop_t * 100, 1)
    return {
        "target_fc_seconds": fc_t,
        "target_total_seconds": drop_t,
        "target_drop_temp": float(drop["target_temp"]) if drop else None,
        "phase_dtr": dtr,
    }


def validate_plan(
    phases: list[dict[str, Any]],
    *,
    charge_temp: float | None,
    batch_size_grams: int | None,
    target_dtr: float | None,
    max_bt_temp: float = 230.0,
    roast_level: str | None = None,
) -> list[str]:
    """Return human-readable warnings for an implausible plan (empty = OK)."""
    from maillard.ai.calibration import get_calibration
    from maillard.roasting.development import plan_drop_conflict

    warning = get_calibration(batch_size_grams)["warning"] if batch_size_grams else None
    warnings: list[str] = [warning] if warning else []
    for phase in phases:
        note = phase.get("notes")
        if isinstance(note, str) and "LOW CONFIDENCE" in note and note not in warnings:
            warnings.append(note)
    times = [p.get("target_time_seconds") for p in phases]
    if len(phases) < 2:
        warnings.append("Plan has fewer than 2 phases; no planned curve")
    if any(t is None for t in times) or any(
        b <= a for a, b in zip(times, times[1:], strict=False) if a is not None and b is not None
    ):
        warnings.append("Phase times are not strictly increasing")
    if batch_size_grams is not None and not 0 < batch_size_grams <= MAX_BATCH_GRAMS:
        warnings.append(f"Batch {batch_size_grams} g outside 1-{MAX_BATCH_GRAMS} g")
    if charge_temp is not None and not (
        CHARGE_TEMP_RANGE[0] <= charge_temp <= CHARGE_TEMP_RANGE[1]
    ):
        warnings.append(
            f"Charge temp {charge_temp:.0f}°C outside "
            f"{CHARGE_TEMP_RANGE[0]:.0f}-{CHARGE_TEMP_RANGE[1]:.0f}°C"
        )
    targets = derive_targets(phases)
    drop_temp = targets["target_drop_temp"]
    if drop_temp is not None and drop_temp > max_bt_temp - 5:
        warnings.append(
            f"Drop temp {drop_temp:.0f}°C is within 5°C of the safety limit {max_bt_temp:.0f}°C"
        )
    total = targets["target_total_seconds"]
    if total is not None and not TOTAL_TIME_RANGE[0] <= total <= TOTAL_TIME_RANGE[1]:
        warnings.append(f"Planned total time {total}s outside {TOTAL_TIME_RANGE}")
    if target_dtr is not None and not DTR_RANGE[0] <= target_dtr <= DTR_RANGE[1]:
        warnings.append(f"Target DTR {target_dtr}% outside {DTR_RANGE[0]}-{DTR_RANGE[1]}%")
    conflict = plan_drop_conflict(drop_temp, roast_level, batch_g=batch_size_grams)
    if conflict:
        warnings.append(conflict)
    return warnings


def build_plan_from_profile(
    profile: Profile,
    *,
    batch_size_grams: int | None = None,
    coffee_id: str | None = None,
    roast_id: str | None = None,
    max_bt_temp: float = 230.0,
    lock: bool = True,
) -> RoastPlan:
    """Snapshot a profile into a RoastPlan (not yet inserted)."""
    from maillard.db.models import RoastPlan

    phases = [dict(p) for p in (profile.phases or [])]
    batch = batch_size_grams if batch_size_grams is not None else profile.batch_size_grams
    charge_temp = profile.charge_temp
    if charge_temp is None:
        charge_phase = _phase(phases, "charge")
        charge_temp = float(charge_phase["target_temp"]) if charge_phase else None
    targets = derive_targets(phases)
    target_dtr = profile.target_dtr if profile.target_dtr is not None else targets["phase_dtr"]
    source = profile.plan_source or ("ai" if profile.ai_rationale else "manual")
    return RoastPlan(
        roast_id=roast_id,
        profile_id=profile.id,
        profile_version=profile.version,
        coffee_id=coffee_id or profile.coffee_id,
        name=profile.name,
        source=source,
        ai_model=profile.ai_model,
        rationale=profile.ai_rationale,
        roast_level=profile.roast_level,
        brew_method=profile.brew_method,
        flavor_goal=profile.flavor_goal,
        batch_size_grams=batch,
        charge_temp=charge_temp,
        target_dtr=target_dtr,
        target_fc_seconds=targets["target_fc_seconds"],
        target_drop_temp=targets["target_drop_temp"],
        target_total_seconds=targets["target_total_seconds"],
        phases=phases,
        planned_curve=interpolate_planned_curve(phases),
        warnings=list(
            dict.fromkeys(
                [
                    *profile.warnings,
                    *validate_plan(
                        phases,
                        charge_temp=charge_temp,
                        batch_size_grams=batch,
                        target_dtr=target_dtr,
                        max_bt_temp=max_bt_temp,
                        roast_level=profile.roast_level,
                    ),
                ]
            )
        ),
        locked_at=datetime.now(UTC).isoformat() if lock else None,
    )


def plan_to_dict(plan: RoastPlan) -> dict[str, Any]:
    """JSON-friendly plan payload for the dashboard."""
    return {
        "id": plan.id,
        "roast_id": plan.roast_id,
        "profile_id": plan.profile_id,
        "name": plan.name,
        "source": plan.source,
        "ai_model": plan.ai_model,
        "rationale": plan.rationale,
        "roast_level": plan.roast_level,
        "brew_method": plan.brew_method,
        "batch_size_grams": plan.batch_size_grams,
        "charge_temp": plan.charge_temp,
        "target_dtr": plan.target_dtr,
        "target_fc_seconds": plan.target_fc_seconds,
        "target_drop_temp": plan.target_drop_temp,
        "target_total_seconds": plan.target_total_seconds,
        "phases": plan.phases,
        "planned_curve": plan.planned_curve,
        "warnings": plan.warnings,
        "locked_at": plan.locked_at,
    }


__all__ = [
    "PLANNED_CURVE_STEP_SECONDS",
    "build_plan_from_profile",
    "derive_targets",
    "interpolate_planned_curve",
    "plan_to_dict",
    "planned_bt_at",
    "validate_plan",
]
