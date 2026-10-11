"""AI-powered roast profile generation.

Generates complete 7-phase roast profiles based on coffee characteristics,
user goals, and optional past roast history. Two tiers:

- Heuristic: always available, adjusts a default template by coffee traits
  and roast goals. No API key needed.
- AI-enhanced: calls Claude to refine the heuristic baseline using
  natural-language reasoning and past roast data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from maillard.ai.calibration import format_calibration, get_calibration, load_calibration
from maillard.ai.history import (
    RoastHistory,
    format_coffee_characteristics,
    format_history_for_prompt,
    load_roast_history,
)
from maillard.ai.utils import parse_llm_json
from maillard.core.config import get_config

if TYPE_CHECKING:
    import anthropic

_logger = logging.getLogger(__name__)

PHASE_NAMES = [
    "charge",
    "turning_point",
    "dry_end",
    "maillard",
    "fc_start",
    "development",
    "drop",
]

# Default requested capacity; calibration explicitly warns above observed batches.
M10_FULL_BATCH_GRAMS = 1200
DEFAULT_BATCH_GRAMS = M10_FULL_BATCH_GRAMS


def _calibrated_phases(batch: int) -> list[dict[str, Any]]:
    c = get_calibration(batch)
    fc = c["fc_time_s"]
    total = fc / (1 - c["development_ratio"])
    dry = c["dry_end_time_s"]
    points = [
        (0, c["charge_temp_c"], 70, 30, None),
        (c["tp_time_s"], c["tp_temp"], 70, 30, None),
        (dry, c["dry_end_temp"], 60, 35, 11),
        ((dry+fc)/2, (c["dry_end_temp"]+c["fc_start_temp"])/2, 50, 40, 9),
        (fc, c["fc_start_temp"], 40, 50, 7),
        ((fc+total)/2, c["fc_start_temp"] + (c["drop_temps"]["medium"]-c["fc_start_temp"])*0.6, 30, 60, 4),
        (total, c["drop_temps"]["medium"], 0, 100, None),
    ]
    return [{"name": name, "target_time_seconds": round(t), "target_temp": round(temp, 1),
                 "heat_percent": heat, "fan_percent": fan, "drum_percent": 60, "target_ror": ror,
                 "notes": c["warning"] if name == "charge" else None}
            for name, (t, temp, heat, fan, ror) in zip(PHASE_NAMES, points, strict=True)]


# DTR targets by brew method
_DTR_TARGETS: dict[str, float] = {
    "filter": 20.0,
    "espresso": 23.0,
    "both": 21.0,
}


@dataclass
class GeneratedProfile:
    """Result of profile generation."""

    name: str
    phases: list[dict[str, Any]]
    charge_temp: float
    target_dtr: float
    ai_rationale: str
    source: str  # "heuristic" or "ai"
    roast_level: str | None = None
    brew_method: str | None = None
    flavor_goal: str | None = None
    batch_size_grams: int | None = None
    adjustments: list[str] = field(default_factory=list)
    ai_model: str | None = None
    warnings: list[str] = field(default_factory=list)


def compute_heuristic_profile(
    history: RoastHistory | None,
    roast_level: str = "Medium",
    flavor_goal: str = "",
    brew_method: str = "Filter",
    batch_size_grams: int = DEFAULT_BATCH_GRAMS,
) -> GeneratedProfile:
    """Compute a roast profile using heuristics only.

    Baseline uses fitted Kaleido BT-probe expectations for the requested batch.

    Starts from the default 7-phase template and adjusts based on:
    - Roast level: drop temp, development time
    - Coffee processing: charge temp adjustments
    - Coffee density: charge temp adjustments
    - Coffee altitude: charge temp adjustments
    - Coffee moisture: drying phase extension
    - Brew method: DTR target

    Args:
        history: Coffee roast history (may be None).
        roast_level: Target roast level.
        flavor_goal: Desired flavor characteristics.
        brew_method: Target brew method.
        batch_size_grams: Batch size in grams.

    Returns:
        GeneratedProfile with heuristic values.
    """
    calibration = get_calibration(batch_size_grams)
    phases = _calibrated_phases(batch_size_grams)
    adjustments: list[str] = []
    charge_temp = calibration["charge_temp_c"]
    if calibration["warning"]:
        adjustments.append(calibration["warning"])

    level_key = roast_level.lower() if roast_level else "medium"
    brew_key = brew_method.lower() if brew_method else "filter"

    # --- Roast level adjustments ---
    drop_temp = calibration["drop_temps"].get(level_key, 205.0)
    drop_source = calibration["sources"].get(f"drop_temps.{level_key}", "default (low confidence)")
    adjustments.append(f"{roast_level} drop: {drop_source}")
    if "low confidence" in drop_source:
        phases[6]["notes"] = f"LOW CONFIDENCE: {roast_level} drop target has no fitted data"
    phases[6]["target_temp"] = drop_temp  # drop phase

    # Scale development phase temp between FC and drop
    fc_temp = float(phases[4]["target_temp"])
    dev_temp = fc_temp + (drop_temp - fc_temp) * 0.6
    phases[5]["target_temp"] = round(dev_temp, 1)  # development phase

    adjustments.append(f"Drop temp {drop_temp}C for {roast_level} roast")

    # --- Coffee characteristics adjustments ---
    if history:
        if history.coffee_processing:
            proc = history.coffee_processing.lower()
            if proc == "natural":
                charge_temp -= 5
                adjustments.append("-5C charge for natural process (scorch risk)")
            elif proc == "honey":
                charge_temp -= 3
                adjustments.append("-3C charge for honey process")

        if history.coffee_density:
            dens = history.coffee_density.lower()
            if dens == "high":
                charge_temp += 5
                adjustments.append("+5C charge for high density")
            elif dens == "low":
                charge_temp -= 5
                adjustments.append("-5C charge for low density")

        if history.coffee_altitude and history.coffee_altitude > 1800:
            charge_temp += 5
            adjustments.append(f"+5C charge for high altitude ({history.coffee_altitude}m)")

        if history.coffee_moisture is not None and history.coffee_moisture > 11.5:
            # Extend drying phase by 15 seconds
            phases[2]["target_time_seconds"] += 15  # dry_end
            # Push subsequent phases back
            for i in range(3, 7):
                phases[i]["target_time_seconds"] += 15
            adjustments.append(
                f"Extended drying +15s for high moisture ({history.coffee_moisture}%)"
            )

        elif history.coffee_moisture is not None and history.coffee_moisture < 9.5:
            for i in range(3):
                phases[i]["heat_percent"] = max(0, phases[i]["heat_percent"] - 3)
            adjustments.append(f"Gentler early heat -3% for low moisture ({history.coffee_moisture}%)")

    # Apply charge temp to charge phase
    phases[0]["target_temp"] = charge_temp

    # --- DTR target ---
    target_dtr = _DTR_TARGETS.get(brew_key, 20.0)
    if brew_key == "espresso":
        adjustments.append(f"DTR target {target_dtr}% for espresso extraction")
    elif brew_key == "filter":
        adjustments.append(f"DTR target {target_dtr}% for filter clarity")

    # --- Development time from DTR (drop = FC / (1 - DTR)) ---
    dtr_for_timing = target_dtr
    if level_key in ("light", "medium-light"):
        dtr_for_timing -= 2.0
        adjustments.append(f"Shorter development for {roast_level} roast")
    elif level_key in ("medium-dark", "dark"):
        dtr_for_timing += 2.0
        adjustments.append(f"Extended development for {roast_level} roast")
    fc_time = phases[4]["target_time_seconds"]
    drop_time = int(round(fc_time / (1 - dtr_for_timing / 100)))
    phases[6]["target_time_seconds"] = drop_time
    phases[5]["target_time_seconds"] = int(round(fc_time + (drop_time - fc_time) * 0.5))

    # --- Generate name ---
    coffee_name = history.coffee_name if history else "Custom"
    name = f"{coffee_name} {roast_level} {brew_method} {batch_size_grams}g"

    # --- Build rationale ---
    if adjustments:
        rationale = "Heuristic profile: " + "; ".join(adjustments) + "."
    else:
        rationale = "Heuristic profile using default template values."

    return GeneratedProfile(
        name=name,
        phases=phases,
        charge_temp=charge_temp,
        target_dtr=target_dtr,
        ai_rationale=rationale,
        source="heuristic",
        roast_level=roast_level,
        brew_method=brew_method,
        flavor_goal=flavor_goal,
        batch_size_grams=batch_size_grams,
        adjustments=adjustments,
    )


class _AIPhase(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    name: str
    target_time_seconds: int = Field(ge=0, le=1200)
    target_temp: float = Field(ge=40, le=260)
    heat_percent: int = Field(ge=0, le=100)
    fan_percent: int = Field(ge=0, le=100)
    drum_percent: int = Field(ge=0, le=100)
    target_ror: float | None = Field(default=None, ge=0, le=40)
    notes: str | None = None

    @field_validator("target_time_seconds", "target_temp", "heat_percent",
                     "fan_percent", "drum_percent", "target_ror", mode="before")
    @classmethod
    def reject_bool(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a numeric target")
        # Models often emit 72.5 / "95.0" for integer targets: round, don't reject.
        if info.field_name in _INT_FIELDS:
            try:
                return round(float(value))
            except (TypeError, ValueError, OverflowError):
                return value
        return value


_INT_FIELDS = {"target_time_seconds", "heat_percent", "fan_percent", "drum_percent"}


class _AIPlan(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    phases: list[_AIPhase] = Field(min_length=7, max_length=7)
    charge_temp: float = Field(ge=150, le=260)
    target_dtr: float = Field(ge=0, le=100)
    name: str | None = None
    rationale: str | None = None

    @field_validator("charge_temp", "target_dtr", mode="before")
    @classmethod
    def reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a numeric target")
        return value


def _validate_ai_phases(phases: Any, batch_size_grams: int = DEFAULT_BATCH_GRAMS) -> bool:
    """Strict phase structure, Celsius sanity and batch-scaled timing checks."""
    try:
        checked = [_AIPhase.model_validate(p) for p in phases]
    except (ValueError, TypeError):
        return False
    if [p.name for p in checked] != PHASE_NAMES:
        return False
    times = [p.target_time_seconds for p in checked]
    fc = get_calibration(batch_size_grams)["fc_time_s"]
    return (
        times[0] == 0
        and all(b > a for a, b in zip(times, times[1:], strict=False))
        and 240 <= times[-1] <= 1200
        and fc * 0.65 <= times[4] <= fc * 1.35
        and 150 <= checked[0].target_temp <= 240
    )


def _validated_ai_plan(parsed: Any, heuristic: GeneratedProfile) -> tuple[_AIPlan, list[str]]:
    plan = _AIPlan.model_validate(parsed)
    if not _validate_ai_phases(
        [p.model_dump() for p in plan.phases],
        heuristic.batch_size_grams or DEFAULT_BATCH_GRAMS,
    ):
        raise ValueError("required phase order, increasing times or calibrated time/temperature bounds")
    temps = [p.target_temp for p in plan.phases[1:]]
    if any(b < a for a, b in zip(temps, temps[1:], strict=False)):
        raise ValueError("BT targets decrease after turning point")
    calibration = get_calibration(heuristic.batch_size_grams or DEFAULT_BATCH_GRAMS)
    warnings = []
    if abs(plan.charge_temp - plan.phases[0].target_temp) > 3:
        plan.charge_temp = plan.phases[0].target_temp
        warnings.append("Charge temperature mismatch repaired from charge phase")
    fc_time = plan.phases[4].target_time_seconds
    drop_time = plan.phases[6].target_time_seconds
    dtr = (drop_time - fc_time) / drop_time * 100
    if abs(plan.target_dtr - dtr) > 0.5:
        plan.target_dtr = round(dtr, 1)
        warnings.append("Target DTR mismatch repaired from FC/drop times")
    fc = plan.phases[4]
    if abs(fc.target_temp - calibration["fc_start_temp"]) > 10:
        fc.target_temp = calibration["fc_start_temp"]
        warnings.append("FC temperature repaired to calibrated expectation")
    expected_drop = calibration["drop_temps"].get(
        (heuristic.roast_level or "Medium").lower(), calibration["drop_temps"]["medium"]
    )
    drop = plan.phases[6]
    if abs(drop.target_temp - expected_drop) > 8:
        drop.target_temp = min(max(drop.target_temp, expected_drop - 8), expected_drop + 8)
        warnings.append("Drop temperature clamped to calibrated roast-level range")
    # Repairs must not leave an internally inconsistent curve.
    temps = [p.target_temp for p in plan.phases[1:]]
    if any(b < a for a, b in zip(temps, temps[1:], strict=False)):
        raise ValueError("temperature repairs would make BT targets decrease after turning point")
    return plan, warnings


async def generate_ai_profile(
    heuristic: GeneratedProfile,
    history: RoastHistory | None,
    client: anthropic.AsyncAnthropic,
    model: str,
) -> GeneratedProfile:
    """Enhance heuristic profile with Claude AI reasoning.

    Args:
        heuristic: The baseline heuristic profile.
        history: Past roast history for context.
        client: Anthropic async client.
        model: Model ID to use.

    Returns:
        AI-enhanced GeneratedProfile, or the heuristic on failure.
    """
    sections = [format_calibration(heuristic.batch_size_grams or DEFAULT_BATCH_GRAMS),
                "Plan within this machine calibration. Preserve low-confidence warnings in phase notes."]

    # Coffee characteristics
    if history:
        sections.append(format_coffee_characteristics(history))
        past_text = format_history_for_prompt(history)
        if past_text:
            sections.append(past_text)

    # User goals
    goals = ["## Roast Goals"]
    if heuristic.roast_level:
        goals.append(f"- Roast level: {heuristic.roast_level}")
    if heuristic.flavor_goal:
        goals.append(f"- Flavor goal: {heuristic.flavor_goal}")
    if heuristic.brew_method:
        goals.append(f"- Brew method: {heuristic.brew_method}")
    if heuristic.batch_size_grams:
        goals.append(f"- Batch size: {heuristic.batch_size_grams}g")
    sections.append("\n".join(goals))

    # Heuristic baseline as reference
    phase_lines = []
    for ph in heuristic.phases:
        m, s = divmod(ph["target_time_seconds"], 60)
        phase_lines.append(
            f"  {ph['name']}: {m}:{s:02d} @ {ph['target_temp']}C "
            f"(H{ph['heat_percent']} F{ph['fan_percent']} D{ph['drum_percent']})"
        )
    sections.append(
        "## Heuristic Baseline\n"
        f"Charge temp: {heuristic.charge_temp}C | DTR target: {heuristic.target_dtr}%\n"
        + "\n".join(phase_lines)
    )

    # Request format
    sections.append(
        "Generate an optimized 7-phase roast profile for a Kaleido M10 roaster "
        f"charged with {heuristic.batch_size_grams or DEFAULT_BATCH_GRAMS} g of green coffee. "
        "You may adjust temperatures, timings, and heat/fan/drum settings from the "
        "heuristic baseline above.\n\n"
        "Respond with ONLY valid JSON, no markdown fences:\n"
        '{"name": "<profile name>", "charge_temp": <float>, "target_dtr": <float>, '
        '"phases": [{"name": "<phase_name>", "target_time_seconds": <int>, '
        '"target_temp": <float>, "heat_percent": <int 0-100>, '
        '"fan_percent": <int 0-100>, "drum_percent": <int 0-100>, '
        '"target_ror": <float or null>, "notes": "<optional>"}], '
        '"rationale": "<2-4 sentences explaining key choices>"}\n\n'
        "Phase names must be exactly: charge, turning_point, dry_end, maillard, "
        "fc_start, development, drop. Times must be monotonically increasing."
    )

    prompt_text = "\n\n".join(sections)

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1536,
            system=(
                "You are an expert coffee roastmaster specializing in the Kaleido M10, "
                "an electric drum roaster run with batches up to 1.2 kg. Generate optimized roast profiles based on coffee "
                "characteristics, past roast data, and user goals. Be specific and "
                "reference concrete metrics from past roasts when available."
            ),
            messages=[{"role": "user", "content": prompt_text}],
        )
        text = response.content[0].text if response.content else ""
        parsed = parse_llm_json(text)

        plan, warnings = _validated_ai_plan(parsed, heuristic)
        ai_phases = [p.model_dump() for p in plan.phases]
        for baseline, phase in zip(heuristic.phases, ai_phases, strict=True):
            if baseline.get("notes") and "LOW CONFIDENCE" in baseline["notes"]:
                phase["notes"] = baseline["notes"]
        return replace(
            heuristic, name=plan.name or heuristic.name, phases=ai_phases,
            charge_temp=plan.charge_temp, target_dtr=plan.target_dtr,
            ai_rationale=plan.rationale or heuristic.ai_rationale,
            source="ai", ai_model=model, warnings=[*heuristic.warnings, *warnings],
        )
    except Exception as exc:
        # Do not expose response values or provider details in operator warnings.
        if isinstance(exc, ValidationError):
            reasons = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()
            )
        elif isinstance(exc, ValueError):
            reasons = str(exc)
        else:
            reasons = "planner response unavailable or malformed"
        warning = f"AI plan rejected: {reasons}; using calibrated heuristic"
        _logger.warning("%s", warning)
        return replace(heuristic, warnings=[*heuristic.warnings, warning])


async def generate_profile(
    coffee_id: str,
    roast_level: str = "Medium",
    flavor_goal: str = "",
    brew_method: str = "Filter",
    batch_size_grams: int = DEFAULT_BATCH_GRAMS,
    client: anthropic.AsyncAnthropic | None = None,
    model: str | None = None,
) -> GeneratedProfile:
    """Top-level orchestrator: load data, compute heuristic, optionally enhance with AI.

    Args:
        coffee_id: Coffee UUID to generate profile for.
        roast_level: Target roast level.
        flavor_goal: Desired flavor characteristics.
        brew_method: Target brew method.
        batch_size_grams: Batch size in grams.
        client: Anthropic client (None = heuristic only).
        model: Model ID for AI enhancement (defaults to configured plan model).

    Returns:
        GeneratedProfile (heuristic or AI-enhanced).
    """
    await load_calibration()
    history = await load_roast_history(coffee_id)

    heuristic = compute_heuristic_profile(
        history=history,
        roast_level=roast_level,
        flavor_goal=flavor_goal,
        brew_method=brew_method,
        batch_size_grams=batch_size_grams,
    )

    if client is not None:
        return await generate_ai_profile(
            heuristic, history, client, model or get_config().ai_plan_model,
        )

    return heuristic
