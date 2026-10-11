"""Map recorded curve events to persisted roast phase fields."""

from collections.abc import Mapping
from math import isfinite
from typing import Any

PHASE_EVENTS = ("turning_point", "dry_end", "fc_start", "fc_end", "sc_start", "drop")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def derive_roast_event_fields(
    curve_data: Mapping[str, Any] | None, *, infer_turning_point: bool = False
) -> dict[str, int | float]:
    """Derive fields from stored events; optionally infer TP within 0–90s.

    TP is inferred only when the BT minimum is at >= 5 s and at least 20 C
    below the first sample (a real post-charge dip).

    Stored events use ``temp``. Legacy event dictionaries with ``bt`` are
    accepted too. Unknown events and invalid numeric values are ignored.
    """
    curve = curve_data or {}
    fields: dict[str, int | float] = {}
    has_tp_event = False
    for event in curve.get("events", []) or []:
        if not isinstance(event, Mapping):
            continue
        phase = event.get("type")
        if phase not in PHASE_EVENTS:
            continue
        has_tp_event |= phase == "turning_point"
        time = event.get("time")
        temp = event.get("temp", event.get("bt"))
        if _number(time) and time >= 0:
            fields.setdefault(f"{phase}_time", int(time))
        if _number(temp):
            fields.setdefault(f"{phase}_temp", temp)
    if infer_turning_point and not has_tp_event:
        samples = [
            (time, bt)
            for time, bt in zip(
                curve.get("time_seconds", []) or [], curve.get("bt", []) or [], strict=False
            )
            if _number(time) and 0 <= time <= 90 and _number(bt)
        ]
        if samples:
            time, bt = min(samples, key=lambda sample: sample[1])
            first_bt = samples[0][1]
            # Only a real post-charge dip counts as a TP: skip test/garbage
            # rows (cold charge, no dip) and minima at the very first sample.
            if time >= 5 and first_bt - bt >= 20:
                fields.update(turning_point_time=int(time), turning_point_temp=bt)
    return fields


def missing_roast_event_fields(roast: Mapping[str, Any]) -> dict[str, int | float]:
    """Return only derivable fields whose current value is absent or null."""
    return {
        field: value
        for field, value in derive_roast_event_fields(
            roast.get("curve_data"), infer_turning_point=True
        ).items()
        if roast.get(field) is None
    }
