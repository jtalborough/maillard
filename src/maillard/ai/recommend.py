"""Pre-roast AI recommendations.

Analyzes coffee characteristics and roast history to suggest optimal
charge temperature and initial control settings before a roast begins.

Two tiers:
- Heuristic: always available, pure function based on past roast data
  and coffee characteristics.
- AI-enhanced: calls Claude to refine heuristic with natural-language reasoning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from maillard.ai.history import RoastHistory, load_roast_history
from maillard.ai.utils import parse_llm_json

if TYPE_CHECKING:
    import anthropic

_logger = logging.getLogger(__name__)

DEFAULT_CHARGE_TEMP = 200.0
DEFAULT_HEAT = 100
DEFAULT_FAN = 30
DEFAULT_DRUM = 50


@dataclass
class PreRoastRecommendation:
    """Recommendation for pre-roast settings."""

    charge_temp: float
    heat_percent: int
    fan_percent: int
    drum_percent: int
    reasoning: str
    source: str  # "heuristic" or "ai"
    confidence: str  # "high", "medium", "low"


def compute_heuristic_recommendation(
    history: RoastHistory | None,
    profile_charge_temp: float | None = None,
) -> PreRoastRecommendation:
    """Compute a recommendation using heuristics only (no AI).

    Priority order:
    1. Best past roast (highest cupping total) -> use its charge_temp
    2. Most recent past roast (no cupping data) -> use its charge_temp
    3. Profile's charge_temp
    4. Coffee characteristics adjustments from 200C base
    5. 200C default

    Args:
        history: Past roast history for the coffee (may be None).
        profile_charge_temp: Charge temp from the selected profile.

    Returns:
        PreRoastRecommendation with heuristic values.
    """
    charge_temp = DEFAULT_CHARGE_TEMP
    confidence = "low"
    reasoning_parts: list[str] = []

    if history and history.past_roasts:
        best = history.best_roast
        if best and best.cupping_avg_total is not None and best.charge_temp is not None:
            # Priority 1: Best past roast with cupping data
            charge_temp = best.charge_temp
            confidence = "high"
            reasoning_parts.append(
                f"Using charge temp {charge_temp:.0f}C from best past roast "
                f"(cupping {best.cupping_avg_total:.0f}/60 avg on {best.date})"
            )
        elif best and best.charge_temp is not None:
            # Priority 2: Most recent past roast (no cupping)
            charge_temp = best.charge_temp
            confidence = "medium"
            reasoning_parts.append(
                f"Using charge temp {charge_temp:.0f}C from most recent past roast ({best.date})"
            )
        elif profile_charge_temp is not None:
            # Priority 3: Profile charge temp
            charge_temp = profile_charge_temp
            confidence = "medium"
            reasoning_parts.append(
                f"Using profile charge temp {charge_temp:.0f}C (no past roast charge data)"
            )
        else:
            # Has history but no charge temps anywhere
            charge_temp = DEFAULT_CHARGE_TEMP
            confidence = "medium"
            reasoning_parts.append(
                f"Using default {charge_temp:.0f}C (past roasts lack charge temp data)"
            )
    elif profile_charge_temp is not None:
        # Priority 3: Profile charge temp (no history)
        charge_temp = profile_charge_temp
        confidence = "medium"
        reasoning_parts.append(
            f"Using profile charge temp {charge_temp:.0f}C (first roast of this coffee)"
        )
    elif history:
        # Priority 4: Coffee characteristics adjustments
        charge_temp = DEFAULT_CHARGE_TEMP
        adjustments: list[str] = []

        has_characteristics = False
        if history.coffee_density:
            has_characteristics = True
            if history.coffee_density.lower() == "high":
                charge_temp += 5
                adjustments.append("+5C for high density")
            elif history.coffee_density.lower() == "low":
                charge_temp -= 5
                adjustments.append("-5C for low density")

        if history.coffee_processing:
            has_characteristics = True
            proc = history.coffee_processing.lower()
            if proc == "natural":
                charge_temp -= 5
                adjustments.append("-5C for natural process (scorch risk)")
            elif proc == "honey":
                charge_temp -= 3
                adjustments.append("-3C for honey process")

        if history.coffee_altitude and history.coffee_altitude > 1800:
            has_characteristics = True
            charge_temp += 5
            adjustments.append(f"+5C for high altitude ({history.coffee_altitude}m)")

        if has_characteristics:
            confidence = "medium"
            base_note = f"Starting from {DEFAULT_CHARGE_TEMP:.0f}C base"
            if adjustments:
                reasoning_parts.append(
                    f"{base_note}, adjusted: {', '.join(adjustments)} = {charge_temp:.0f}C"
                )
            else:
                reasoning_parts.append(f"{base_note} (no adjustments needed)")
        else:
            confidence = "low"
            reasoning_parts.append(
                f"Using default {charge_temp:.0f}C (no history or characteristics available)"
            )
    else:
        # Priority 5: Bare default
        confidence = "low"
        reasoning_parts.append(
            f"Using default {charge_temp:.0f}C (no coffee data available)"
        )

    return PreRoastRecommendation(
        charge_temp=charge_temp,
        heat_percent=DEFAULT_HEAT,
        fan_percent=DEFAULT_FAN,
        drum_percent=DEFAULT_DRUM,
        reasoning=". ".join(reasoning_parts),
        source="heuristic",
        confidence=confidence,
    )


async def generate_ai_recommendation(
    heuristic: PreRoastRecommendation,
    history: RoastHistory | None,
    profile_info: dict[str, Any] | None,
    client: anthropic.AsyncAnthropic,
    model: str,
) -> PreRoastRecommendation:
    """Enhance heuristic recommendation with Claude AI reasoning.

    Args:
        heuristic: The base heuristic recommendation.
        history: Past roast history for context.
        profile_info: Profile details dict (name, roast_level, etc.).
        client: Anthropic async client.
        model: Model ID to use.

    Returns:
        AI-enhanced PreRoastRecommendation.
    """
    from maillard.ai.history import format_coffee_characteristics, format_history_for_prompt

    sections = []

    # Coffee info
    if history:
        sections.append(format_coffee_characteristics(history))
        past_text = format_history_for_prompt(history)
        if past_text:
            sections.append(past_text)

    # Profile info
    if profile_info:
        parts = []
        if profile_info.get("name"):
            parts.append(f"Profile: {profile_info['name']}")
        if profile_info.get("roast_level"):
            parts.append(f"Target: {profile_info['roast_level']}")
        if profile_info.get("charge_temp"):
            parts.append(f"Profile charge temp: {profile_info['charge_temp']}C")
        if parts:
            sections.append("## Selected Profile\n" + " | ".join(parts))

    # Heuristic baseline
    sections.append(
        f"## Heuristic Recommendation\n"
        f"Charge temp: {heuristic.charge_temp:.0f}C | "
        f"Heat: {heuristic.heat_percent}% | Fan: {heuristic.fan_percent}% | "
        f"Drum: {heuristic.drum_percent}%\n"
        f"Confidence: {heuristic.confidence}\n"
        f"Reasoning: {heuristic.reasoning}"
    )

    sections.append(
        "Review the heuristic recommendation above. You may adjust the charge "
        "temperature and initial heat/fan/drum settings based on the coffee "
        "characteristics and past roast history.\n\n"
        "Respond with ONLY valid JSON, no markdown fences:\n"
        '{"charge_temp": <float>, "heat_percent": <int 0-100>, '
        '"fan_percent": <int 0-100>, "drum_percent": <int 0-100>, '
        '"reasoning": "<1-3 sentences explaining your recommendation>"}'
    )

    prompt_text = "\n\n".join(sections)

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=256,
            system=(
                "You are an expert coffee roastmaster. Recommend optimal pre-roast "
                "settings (charge temperature, heat, fan, drum) based on coffee "
                "characteristics and past roast data. Be specific and concise."
            ),
            messages=[{"role": "user", "content": prompt_text}],
        )
        text = response.content[0].text if response.content else ""
        parsed = parse_llm_json(text)
        if parsed:
            return PreRoastRecommendation(
                charge_temp=float(parsed.get("charge_temp", heuristic.charge_temp)),
                heat_percent=int(parsed.get("heat_percent", heuristic.heat_percent)),
                fan_percent=int(parsed.get("fan_percent", heuristic.fan_percent)),
                drum_percent=int(parsed.get("drum_percent", heuristic.drum_percent)),
                reasoning=parsed.get("reasoning", heuristic.reasoning),
                source="ai",
                confidence=heuristic.confidence,
            )
    except Exception:
        _logger.warning("AI recommendation failed, falling back to heuristic", exc_info=True)

    return heuristic


async def get_recommendation(
    coffee_id: str,
    profile_id: str | None = None,
    client: anthropic.AsyncAnthropic | None = None,
    model: str | None = None,
) -> PreRoastRecommendation:
    """Top-level orchestrator: load data, compute heuristic, optionally enhance with AI.

    Args:
        coffee_id: Coffee UUID to recommend for.
        profile_id: Optional profile UUID for additional context.
        client: Anthropic client (None = heuristic only).
        model: Model ID for AI enhancement.

    Returns:
        PreRoastRecommendation (heuristic or AI-enhanced).
    """
    # Load roast history
    history = await load_roast_history(coffee_id)

    # Load profile charge_temp if provided
    profile_charge_temp: float | None = None
    profile_info: dict[str, Any] | None = None
    if profile_id:
        try:
            from maillard.db.repository import ProfileRepository

            profile_repo = ProfileRepository()
            profile = await profile_repo.get_by_id(profile_id)
            if profile:
                profile_charge_temp = profile.charge_temp
                profile_info = {
                    "name": profile.name,
                    "roast_level": profile.roast_level,
                    "charge_temp": profile.charge_temp,
                }
        except Exception:
            _logger.warning("Failed to load profile %s", profile_id, exc_info=True)

    # Compute heuristic
    heuristic = compute_heuristic_recommendation(history, profile_charge_temp)

    # Enhance with AI if client available
    if client is not None and model is not None:
        return await generate_ai_recommendation(
            heuristic, history, profile_info, client, model
        )

    return heuristic
