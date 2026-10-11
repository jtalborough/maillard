"""Machine/probe expectations, fitted read-only from supervised roast history."""

from __future__ import annotations

import logging
import math
import time
from statistics import median
from typing import Any

from pydantic import BaseModel, Field

from maillard.roasting.events import derive_roast_event_fields


class LinearModel(BaseModel):
    a: float
    b: float
    min_g: float = 450
    max_g: float = 808
    # Physics prior for extrapolating above the largest observed batch: a
    # bigger bean mass needs at least this much more per gram (e.g. charge).
    extrapolation_min_slope: float = 0.0

    def at(self, grams: float) -> float:
        if grams > self.max_g and self.extrapolation_min_slope > self.b:
            edge = self.a + self.b * self.max_g
            return edge + self.extrapolation_min_slope * (grams - self.max_g)
        return self.a + self.b * grams


class RoasterCalibration(BaseModel):
    tp_time_s: float = 40
    tp_temp: float = 88
    dry_end_temp: float = 155
    dry_end_fc_ratio: float = 0.62
    fc_start_temp: float = 182
    # Defaults = Theil-Sen fit of the author's 16 usable Kaleido roasts (300-800 g,
    # 2026-02..05); the live app refits from the DB at plan time.
    fc_time: LinearModel = Field(
        default_factory=lambda: LinearModel(a=212, b=0.51, min_g=300, max_g=800)
    )
    charge_temp: LinearModel = Field(
        default_factory=lambda: LinearModel(
            a=199, b=0.0, min_g=300, max_g=800, extrapolation_min_slope=0.025
        )
    )
    total_time: LinearModel = Field(
        default_factory=lambda: LinearModel(a=281, b=0.63, min_g=300, max_g=800)
    )
    drop_temps: dict[str, float] = Field(
        default_factory=lambda: {
            "light": 201,
            "medium-light": 203,
            "medium": 205,
            "medium-dark": 209,
            "dark": 214,
        }
    )
    development_ratio: float = 0.22
    drop_ror_range: tuple[float, float] = (2, 4)
    min_maillard_ror: float = 3
    max_observed_et: float = 200
    accepted_roasts: int = 0
    excluded_roasts: int = 0
    sources: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        for name in type(self).model_fields:
            if name not in ("sources", "accepted_roasts", "excluded_roasts"):
                self.sources.setdefault(name, "default (low confidence)")
        for level in self.drop_temps:
            self.sources.setdefault(f"drop_temps.{level}", "default (low confidence)")

    def expectations(self, batch_g: float) -> dict[str, Any]:
        low, high = self.fc_time.min_g, self.fc_time.max_g
        extrapolated = not low <= batch_g <= high
        warning = None
        if batch_g > high:
            warning = f"LOW CONFIDENCE: extrapolated beyond {high:g} g max observed"
        elif batch_g < low:
            warning = f"LOW CONFIDENCE: extrapolated below {low:g} g min observed"
        data = self.model_dump()
        for field in ("fc_time", "charge_temp", "total_time"):
            model = getattr(self, field)
            if not model.min_g <= batch_g <= model.max_g:
                data["sources"][field] = "extrapolated"
        return {
            **data,
            "batch_g": batch_g,
            "fc_time_s": self.fc_time.at(batch_g),
            "charge_temp_c": self.charge_temp.at(batch_g),
            "total_time_s": self.total_time.at(batch_g),
            "dry_end_time_s": self.fc_time.at(batch_g) * self.dry_end_fc_ratio,
            "extrapolated": extrapolated,
            "warning": warning,
            "batch_source": "extrapolated" if extrapolated else self.sources["fc_time"],
        }


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def roast_quality_issues(roast: Any) -> list[str]:
    """Shared eligibility check for history, calibration and cleanup.

    Missing charge temperatures are allowed; a charge below 100C is a cold
    test run. Late charge marks (100-150C) still carry useful FC/TP data.
    Late drop temperatures are ignored by the fit, not grounds to lose FC data.
    """
    row = dict(roast) if isinstance(roast, dict) else roast.model_dump()
    curve = row.get("curve_data") or {"events": row.get("events", [])}
    for key, value in derive_roast_event_fields(curve).items():
        if row.get(key) is None:
            row[key] = value
    issues = []
    if row.get("excluded"):
        issues.append("discarded by operator")
    if row.get("status") != "completed":
        issues.append("not completed")
    total = row.get("total_time_seconds")
    if total is None:
        total = row.get("drop_time")
    if not _number(total) or not 180 <= total <= 1500:
        issues.append("missing or absurd duration (expected 180-1500s)")
    drop = row.get("drop_time")
    if drop is not None and (not _number(drop) or not 180 <= drop <= 1500):
        issues.append("absurd drop time")
    batch = row.get("batch_size_grams")
    if not _number(batch) or batch < 250:
        issues.append("missing or test-size batch (<250g)")
    charge = row.get("charge_temp")
    if charge is not None and (not _number(charge) or charge < 100):
        issues.append("cold or invalid charge (<100C)")
    if row.get("is_test") or row.get("test_roast"):
        issues.append("test roast")
    return issues


def fit_calibration(roasts: Any) -> RoasterCalibration:
    result = RoasterCalibration()
    rows = []
    for roast in roasts:
        row = dict(roast) if isinstance(roast, dict) else roast.model_dump()
        curve = row.get("curve_data") or {"events": row.get("events", [])}
        for key, value in derive_roast_event_fields(curve, infer_turning_point=True).items():
            if row.get(key) is None:
                row[key] = value
        total = row.get("total_time_seconds")
        if total is None:
            total = row.get("drop_time")
        if roast_quality_issues(row):
            result.excluded_roasts += 1
            continue
        row["total"] = total
        tp = row.get("curve_tp_min_bt")
        if tp and len(tp) == 2 and all(_number(v) for v in tp) and 5 <= tp[0] <= 90:
            if row.get("turning_point_time") is None:
                row["turning_point_time"] = tp[0]
            if row.get("turning_point_temp") is None:
                row["turning_point_temp"] = tp[1]
        rows.append(row)
    result.accepted_roasts = len(rows)

    def stat(field: str, values: list[float]) -> None:
        if values:
            setattr(result, field, float(median(values)))
            result.sources[field] = f"fitted (n={len(values)})"

    for field, key in (
        ("tp_time_s", "turning_point_time"),
        ("tp_temp", "turning_point_temp"),
        ("dry_end_temp", "dry_end_temp"),
    ):
        stat(field, [r[key] for r in rows if _number(r.get(key))])
    fc_rows = [
        r
        for r in rows
        if _number(r.get("fc_start_temp"))
        and 165 <= r["fc_start_temp"] <= 205
        and _number(r.get("fc_start_time"))
        and 90 < r["fc_start_time"] < r["total"]
    ]
    stat("fc_start_temp", [r["fc_start_temp"] for r in fc_rows])
    stat("development_ratio", [(r["total"] - r["fc_start_time"]) / r["total"] for r in fc_rows])
    stat(
        "dry_end_fc_ratio",
        [
            r["dry_end_time"] / r["fc_start_time"]
            for r in fc_rows
            if _number(r.get("dry_end_time")) and 90 < r["dry_end_time"] < r["fc_start_time"]
        ],
    )

    def line(field: str, data: list[dict], key: str) -> None:
        if len(data) < 3 or len({r["batch_size_grams"] for r in data}) < 2:
            return
        xs = [r["batch_size_grams"] for r in data]
        ys = [r[key] for r in data]
        # Theil-Sen: robust to the odd very long/short roast in a small sample.
        slopes = [
            (ys[j] - ys[i]) / (xs[j] - xs[i])
            for i in range(len(xs))
            for j in range(i + 1, len(xs))
            if xs[j] != xs[i]
        ]
        b = float(median(slopes))
        a = float(median(y - b * x for x, y in zip(xs, ys, strict=True)))
        prior = getattr(result, field).extrapolation_min_slope
        setattr(result, field, LinearModel(a=a, b=b, min_g=min(xs), max_g=max(xs),
                                           extrapolation_min_slope=prior))
        result.sources[field] = f"fitted (n={len(data)}, Theil-Sen)"

    line("fc_time", fc_rows, "fc_start_time")
    # A charge recorded below 150 C is a late/cold charge mark, not a real
    # charge temperature; keep those roasts for FC/TP but not for charge.
    line("charge_temp", [r for r in rows if _number(r.get("charge_temp")) and r["charge_temp"] >= 150],
         "charge_temp")
    line("total_time", rows, "total")
    for level in result.drop_temps:
        values = [
            r["drop_temp"]
            for r in rows
            if str(r.get("roast_level", "")).lower() == level
            and _number(r.get("drop_temp"))
            and 190 <= r["drop_temp"] <= 225
            and (not _number(r.get("fc_end_temp")) or r["drop_temp"] >= r["fc_end_temp"])
        ]
        if values:
            result.drop_temps[level] = float(median(values))
            result.sources[f"drop_temps.{level}"] = f"fitted (n={len(values)})"
    _enforce_monotonic_drops(result)
    ets, rors = [], []
    for row in rows:
        curve = row.get("curve_data") or {}
        ets.extend(v for v in curve.get("et", []) if _number(v))
        if _number(row.get("max_et")):
            ets.append(row["max_et"])
        rors.extend(
            v
            for t, v in zip(curve.get("time_seconds", []), curve.get("bt_ror", []), strict=False)
            if _number(t) and row["total"] - 30 <= t <= row["total"] and _number(v) and 0 <= v <= 15
        )
    if ets:
        result.max_observed_et = max(ets)
        result.sources["max_observed_et"] = f"fitted (n={len(rows)})"
    if rors:
        center = median(rors)
        result.drop_ror_range = (max(0, center - 1), center + 1)
        result.sources["drop_ror_range"] = f"fitted (n={len(rors)})"
    return result


def _enforce_monotonic_drops(cal: RoasterCalibration) -> None:
    """Darker levels must never target a lower drop temp than lighter ones.

    the author's level labels are noisy (e.g. fitted Light > Medium), so adjacent
    violators are pooled (pool-adjacent-violators, weighted by sample count).
    """
    levels = list(cal.drop_temps)

    def weight(level: str) -> int:
        src = cal.sources.get(f"drop_temps.{level}", "")
        if src.startswith("fitted (n="):
            return int(src.split("n=")[1].split(")")[0].split(",")[0])
        return 1

    blocks: list[list[Any]] = []  # [mean, weight, [levels]]
    for level in levels:
        blocks.append([cal.drop_temps[level], weight(level), [level]])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m2, w2, l2 = blocks.pop()
            m1, w1, l1 = blocks.pop()
            blocks.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, l1 + l2])
    for mean, _, group in blocks:
        for level in group:
            if len(group) > 1:
                cal.sources[f"drop_temps.{level}"] += " (pooled for monotonic levels)"
            cal.drop_temps[level] = round(float(mean), 1)


_cached = RoasterCalibration()
_loaded_at = 0.0


def get_calibration(batch_g: float) -> dict[str, Any]:
    return _cached.expectations(batch_g)


async def load_calibration(*, refresh: bool = False) -> RoasterCalibration:
    """Cache DB fits for five minutes; failures use documented defaults."""
    global _cached, _loaded_at
    if not refresh and _loaded_at and time.monotonic() - _loaded_at < 300:
        return _cached
    try:
        from maillard.db.models import Roast

        _cached = fit_calibration(await Roast.find({"status": "completed"}).to_list())
    except Exception:
        logging.getLogger(__name__).warning("Calibration unavailable; using defaults")
        _cached = RoasterCalibration()
    _loaded_at = time.monotonic()
    return _cached


def format_calibration(batch_g: float) -> str:
    c = get_calibration(batch_g)
    drops = ", ".join(
        f"{k} {v:.1f}C ({c['sources']['drop_temps.' + k]})" for k, v in c["drop_temps"].items()
    )
    return (
        f"## Kaleido BT probe calibration ({batch_g:g} g)\n"
        f"TP ~{c['tp_time_s']:.0f}s @ {c['tp_temp']:.1f}C; dry end ~{c['dry_end_time_s']:.0f}s @ {c['dry_end_temp']:.1f}C; "
        f"FC ~{c['fc_time_s']:.0f}s @ {c['fc_start_temp']:.1f}C ({c['sources']['fc_start_temp']}); "
        f"charge ~{c['charge_temp_c']:.1f}C; total ~{c['total_time_s']:.0f}s; typical DTR {c['development_ratio'] * 100:.1f}%.\n"
        f"Drop targets: {drops}. Near-drop RoR {c['drop_ror_range'][0]:.1f}-{c['drop_ror_range'][1]:.1f}C/min. "
        f"Max observed ET {c['max_observed_et']:.1f}C (ET reads below BT late).\n"
        + (
            c["warning"]
            or "Expectations are guides; trust operator events and sensory observations."
        )
    )
