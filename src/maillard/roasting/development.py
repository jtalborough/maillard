"""Deterministic first-crack, development, and drop limits around the AI."""

from __future__ import annotations

from dataclasses import dataclass

from maillard.ai.calibration import get_calibration

APPROACH_C = 5.0
HEAT_STEP = 5
SC_PROMPT_C = 4.0
SC_HEAT_ZERO_C = 6.0
DROP_CONFLICT_C = 4.0
FAR_BELOW_LEVEL_C = 15.0

_LEVELS: dict[str, tuple[tuple[float, float], tuple[int, int]]] = {
    "light": ((18.0, 20.0), (90, 150)),
    "medium-light": ((20.0, 22.0), (100, 165)),
    "medium": ((21.0, 24.0), (110, 180)),
    "medium-dark": ((23.0, 26.0), (120, 190)),
    "dark": ((25.0, 28.0), (130, 200)),
}


def normalize_level(level: str | None) -> str:
    if not level:
        return "medium"
    key = level.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {"mediumlight": "medium-light", "mediumdark": "medium-dark"}
    key = aliases.get(key, key)
    return key if key in _LEVELS else "medium"


def drop_target_c(
    level: str | None,
    calibration: dict | None = None,
    batch_g: float | None = None,
) -> float:
    """Calibrated drop temperature for a roast level. This is the only drop target."""
    cal = calibration if calibration is not None else get_calibration(batch_g or 0)
    temps = cal["drop_temps"]
    key = normalize_level(level)
    if key not in temps:
        key = "medium" if "medium" in temps else next(iter(temps))
    return float(temps[key])


def predicted_fc_c(calibration: dict | None = None, batch_g: float | None = None) -> float:
    cal = calibration if calibration is not None else get_calibration(batch_g or 0)
    return float(cal["fc_start_temp"])


def development_band(level: str | None) -> tuple[tuple[float, float], tuple[int, int]]:
    return _LEVELS[normalize_level(level)]


def plan_drop_conflict(
    plan_drop: float | None,
    level: str | None,
    calibration: dict | None = None,
    batch_g: float | None = None,
) -> str | None:
    if plan_drop is None or not level:
        return None
    target = drop_target_c(level, calibration, batch_g)
    if abs(float(plan_drop) - target) > DROP_CONFLICT_C:
        name = normalize_level(level)
        return (
            f"Plan drop {float(plan_drop):.0f}C conflicts with {name} "
            f"target {target:.0f}C and is rejected"
        )
    return None


@dataclass(frozen=True)
class DevSample:
    bt: float
    ror: float | None
    elapsed: float | None
    heat: int
    fan: int
    drum: int
    ai_heat: int | None
    ai_fan: int | None
    ai_drum: int | None
    fc_predicted: float
    fc_marked: bool
    fc_elapsed: float | None
    drop_target: float
    dev_min_s: float
    dev_max_s: float
    dtr_low: float
    dtr_high: float


@dataclass(frozen=True)
class DevCommand:
    heat: int
    fan: int
    drum: int
    drop_prompt: bool
    forced_prompt: bool
    zero_heat: bool
    auto_drop: bool
    constrained: bool
    reason: str


def _bound(value: int) -> int:
    return max(0, min(100, int(value)))


class DevelopmentController:
    """Steps heat down into first crack and refuses heat increases after it."""

    def __init__(self) -> None:
        self._last_ror: float | None = None
        self._drop_sent = False
        self._forced_sent = False

    def reset(self) -> None:
        self._last_ror = None
        self._drop_sent = False
        self._forced_sent = False

    def step(self, sample: DevSample, *, auto_drop: bool = False) -> DevCommand:
        current = _bound(sample.heat)
        ai_heat = current if sample.ai_heat is None else _bound(sample.ai_heat)
        ai_fan = sample.fan if sample.ai_fan is None else _bound(sample.ai_fan)
        ai_drum = sample.drum if sample.ai_drum is None else _bound(sample.ai_drum)
        in_approach = (not sample.fc_marked) and sample.bt >= sample.fc_predicted - APPROACH_C
        after_fc = sample.fc_marked
        zero = sample.bt >= sample.drop_target + SC_HEAT_ZERO_C
        forced = sample.bt >= sample.drop_target + SC_PROMPT_C
        declining = (
            self._last_ror is not None
            and sample.ror is not None
            and sample.ror <= self._last_ror - 0.2
        )
        if sample.ror is not None:
            self._last_ror = sample.ror

        heat = ai_heat
        reason = ""
        constrained = False
        if zero:
            heat = 0
            constrained = True
            reason = "second-crack guard"
        elif after_fc or in_approach:
            ceiling = current if declining else max(0, current - HEAT_STEP)
            heat = min(heat, ceiling, current)
            constrained = True
            reason = "after first crack" if after_fc else "approaching first crack"

        dev_s = None
        dtr = None
        if after_fc and sample.fc_elapsed is not None and sample.elapsed is not None:
            dev_s = sample.elapsed - sample.fc_elapsed
            if sample.elapsed > 0:
                dtr = dev_s / sample.elapsed * 100.0
        temp_hit = sample.bt >= sample.drop_target
        window_hit = (
            dev_s is not None
            and dtr is not None
            and dev_s >= sample.dev_min_s
            and dtr >= sample.dtr_low
            and sample.bt >= sample.drop_target - 3
        )
        target_reached = temp_hit or window_hit or (dev_s is not None and dev_s >= sample.dev_max_s)

        drop_prompt = False
        do_auto = False
        if target_reached and not self._drop_sent:
            self._drop_sent = True
            drop_prompt = True
            do_auto = bool(auto_drop) and not zero
        forced_prompt = False
        if forced and not self._forced_sent:
            self._forced_sent = True
            forced_prompt = True
            drop_prompt = True

        return DevCommand(
            heat=heat,
            fan=_bound(ai_fan),
            drum=_bound(ai_drum),
            drop_prompt=drop_prompt,
            forced_prompt=forced_prompt,
            zero_heat=zero,
            auto_drop=do_auto,
            constrained=constrained,
            reason=reason,
        )
