"""Past roast history loading and formatting for AI context.

Loads completed roasts for a coffee, builds lightweight summaries,
and formats them for injection into the AI agent's prompt context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from maillard.db.aggregation import aggregate_cuppings
from maillard.db.models import Roast

_logger = logging.getLogger(__name__)


def _fmt_time(seconds: int | None) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


@dataclass
class PastRoastSummary:
    """Lightweight summary of a completed roast."""

    roast_id: str
    date: str  # ISO date from start_time
    charge_temp: float | None
    turning_point_time: int | None
    turning_point_temp: float | None
    dry_end_time: int | None
    dry_end_temp: float | None
    fc_start_time: int | None
    fc_start_temp: float | None
    drop_temp: float | None
    drop_time: int | None
    dtr: float | None
    total_time_seconds: int | None
    cupping_avg_total: float | None  # mean of per-cupping totals
    cupping_count: int = 0
    ranked_flavor_notes: list[str] = field(default_factory=list)
    cupping_notes: str | None = None
    next_time_notes: str | None = None


@dataclass
class RoastHistory:
    """Past roast history for a coffee."""

    coffee_name: str
    coffee_origin: str
    coffee_processing: str | None
    coffee_altitude: int | None
    coffee_density: str | None
    coffee_moisture: float | None = None
    coffee_density_g_l: float | None = None
    past_roasts: list[PastRoastSummary] = field(default_factory=list)
    best_roast: PastRoastSummary | None = None


def _build_summary(roast: Roast, cuppings: Any = None) -> PastRoastSummary:
    """Build a PastRoastSummary from a Roast document.

    Args:
        roast: Roast document.
        cuppings: Pre-loaded cuppings for this roast (MongoDB has no ORM relations).
    """
    date = ""
    if roast.start_time:
        date = roast.start_time[:10]  # Extract YYYY-MM-DD

    agg = aggregate_cuppings(cuppings or [])

    def feedback(key: str) -> str | None:
        values = [getattr(c, key, None) for c in (cuppings or [])]
        return " ".join(" ".join(v.split()) for v in values if isinstance(v, str) and v.strip())[:240] or None

    return PastRoastSummary(
        roast_id=roast.id,
        date=date,
        charge_temp=roast.charge_temp,
        turning_point_time=roast.turning_point_time,
        turning_point_temp=roast.turning_point_temp,
        dry_end_time=roast.dry_end_time,
        dry_end_temp=roast.dry_end_temp,
        fc_start_time=roast.fc_start_time,
        fc_start_temp=roast.fc_start_temp,
        drop_temp=roast.drop_temp,
        drop_time=roast.drop_time,
        dtr=roast.dtr,
        total_time_seconds=roast.total_time_seconds,
        cupping_avg_total=agg.avg_total,
        cupping_count=agg.count,
        ranked_flavor_notes=agg.ranked_flavor_notes,
        cupping_notes=feedback("notes"),
        next_time_notes=feedback("next_time_notes"),
    )


def _determine_best(summaries: list[PastRoastSummary]) -> PastRoastSummary | None:
    """Determine the best roast: highest avg cupping total, then most recent."""
    if not summaries:
        return None
    # Sort by cupping_avg_total desc (None last), then by date desc
    scored = [s for s in summaries if s.cupping_avg_total is not None]
    if scored:
        return max(scored, key=lambda s: (s.cupping_avg_total or 0, s.date))
    # No cupping data — return most recent
    return summaries[0]


async def load_roast_history(
    coffee_id: str,
    limit: int = 5,
) -> RoastHistory | None:
    """Load past roast history for a coffee from the database.

    Args:
        coffee_id: The coffee UUID to load history for.
        limit: Maximum number of past roasts to load.

    Returns:
        RoastHistory with past roast summaries, or None if coffee not found.
    """
    from maillard.db.repository import CoffeeRepository, CuppingRepository, RoastRepository

    try:
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.get_by_id(coffee_id)
        if coffee is None:
            return None

        roast_repo = RoastRepository()
        roasts = await roast_repo.get_completed_by_coffee_id(
            coffee_id, limit=limit
        )

        # Load cuppings per roast (MongoDB has no ORM relationships)
        cupping_repo = CuppingRepository()
        summaries = []
        for r in roasts:
            cuppings = await cupping_repo.get_by_roast_id(r.id)
            summaries.append(_build_summary(r, cuppings))

        best = _determine_best(summaries)

        return RoastHistory(
            coffee_name=coffee.name,
            coffee_origin=coffee.origin,
            coffee_processing=coffee.processing,
            coffee_altitude=coffee.altitude_m if coffee.altitude_m is not None else coffee.altitude,
            coffee_density=coffee.density,
            coffee_moisture=coffee.moisture_pct if coffee.moisture_pct is not None else coffee.moisture,
            coffee_density_g_l=coffee.density_g_l,
            past_roasts=summaries,
            best_roast=best,
        )
    except Exception:
        _logger.warning("Failed to load roast history for coffee %s", coffee_id, exc_info=True)
        return None


def format_history_for_prompt(history: RoastHistory | None) -> str | None:
    """Format roast history as concise text for LLM prompt injection.

    Returns None if no history or no past roasts.
    Target: ~300 tokens for 5 roasts.
    """
    if history is None or not history.past_roasts:
        return None

    lines = ["## Past Roasts (same coffee)"]

    # Best roast first (if cupping data exists)
    if history.best_roast and history.best_roast.cupping_avg_total is not None:
        b = history.best_roast
        notes = ", ".join(b.ranked_flavor_notes[:4]) if b.ranked_flavor_notes else "no notes"
        # Build the best line
        parts = [f"BEST: {b.date}"]
        if b.charge_temp is not None:
            parts.append(f"charge {b.charge_temp:.0f}C")
        if b.dtr is not None:
            parts.append(f"DTR {b.dtr:.0f}%")
        if b.drop_temp is not None:
            parts.append(f"drop {b.drop_temp:.0f}C")
        if b.fc_start_time is not None:
            parts.append(f"FC@ {_fmt_time(b.fc_start_time)}")
        parts.append(f"total {_fmt_time(b.total_time_seconds)}")
        if b.cupping_avg_total is not None:
            cupping_str = f"cupping {b.cupping_avg_total:.0f}/60 avg"
            if b.cupping_count > 1:
                cupping_str += f" (n={b.cupping_count})"
            parts.append(cupping_str)
        if b.ranked_flavor_notes:
            parts.append(f"({notes})")
        lines.append(" | ".join(parts))

    def feedback_parts(summary: PastRoastSummary) -> list[str]:
        parts = []
        if summary.cupping_notes:
            parts.append(f"last time: {summary.cupping_notes}")
        if summary.next_time_notes:
            parts.append(f"next time try: {summary.next_time_notes}")
        return parts

    if history.best_roast and history.best_roast.cupping_avg_total is not None:
        lines[-1] += "".join(f" | {p}" for p in feedback_parts(history.best_roast))

    # Other roasts
    for s in history.past_roasts:
        if history.best_roast and history.best_roast.cupping_avg_total is not None and s.roast_id == history.best_roast.roast_id:
            continue  # Already shown as BEST
        parts = [s.date]
        if s.charge_temp is not None:
            parts.append(f"charge {s.charge_temp:.0f}C")
        if s.dtr is not None:
            parts.append(f"DTR {s.dtr:.0f}%")
        if s.drop_temp is not None:
            parts.append(f"drop {s.drop_temp:.0f}C")
        if s.fc_start_time is not None:
            parts.append(f"FC@ {_fmt_time(s.fc_start_time)}")
        parts.append(f"total {_fmt_time(s.total_time_seconds)}")
        if s.cupping_avg_total is not None:
            cupping_str = f"cupping {s.cupping_avg_total:.0f}/60 avg"
            if s.cupping_count > 1:
                cupping_str += f" (n={s.cupping_count})"
            parts.append(cupping_str)
        if s.ranked_flavor_notes:
            notes_str = ", ".join(s.ranked_flavor_notes[:3])
            parts.append(f"({notes_str})")
        parts.extend(feedback_parts(s))
        lines.append(" | ".join(parts))

    lines.append("")
    lines.append(
        "Compare your current pace and temps to the BEST roast above. "
        "Reference specific metrics from past roasts in your reasoning."
    )

    return "\n".join(lines)


def format_comparison_data(
    current_metrics: dict[str, Any],
    history: RoastHistory | None,
) -> str:
    """Build a comparison prompt for post-roast LLM analysis.

    Args:
        current_metrics: Dict with drop_temp, total_time_seconds, dtr, etc.
        history: Past roast history (may be None or empty).

    Returns:
        Formatted prompt text for post-roast comparison.
    """
    lines = ["## Post-Roast Comparison"]

    # Current roast metrics
    lines.append("\n### This Roast")
    if current_metrics.get("drop_temp") is not None:
        lines.append(f"- Drop temp: {current_metrics['drop_temp']:.0f}C")
    if current_metrics.get("total_time_seconds") is not None:
        lines.append(f"- Total time: {_fmt_time(current_metrics['total_time_seconds'])}")
    if current_metrics.get("dtr") is not None:
        lines.append(f"- DTR: {current_metrics['dtr']:.1f}%")
    if current_metrics.get("fc_start_time") is not None:
        lines.append(f"- FC start: {_fmt_time(current_metrics['fc_start_time'])}")

    # Past roasts for comparison
    if history and history.past_roasts:
        lines.append("\n### Past Roasts")
        if history.best_roast:
            b = history.best_roast
            parts = []
            if b.drop_temp is not None:
                parts.append(f"drop {b.drop_temp:.0f}C")
            parts.append(f"total {_fmt_time(b.total_time_seconds)}")
            if b.dtr is not None:
                parts.append(f"DTR {b.dtr:.0f}%")
            lines.append(f"Best ({b.date}): " + ", ".join(parts))
            if b.cupping_avg_total is not None:
                cupping_str = f"  Cupping: {b.cupping_avg_total:.0f}/60 avg"
                if b.cupping_count > 1:
                    cupping_str += f" (n={b.cupping_count})"
                lines.append(cupping_str)
            if b.ranked_flavor_notes:
                lines.append(f"  Notes: {', '.join(b.ranked_flavor_notes[:4])}")
    else:
        lines.append("\nNo past roasts to compare against (first roast of this coffee).")

    lines.append(
        "\nAnalyze: What improved vs the best past roast? What regressed? "
        "What flavor impact do you predict based on the differences? "
        "Give specific, actionable suggestions for the next roast."
    )

    return "\n".join(lines)


def format_coffee_characteristics(history: RoastHistory) -> str:
    """Format coffee characteristics for first-roast plan (no history available)."""
    lines = ["## Coffee Characteristics"]
    lines.append(f"- Name: {history.coffee_name}")
    lines.append(f"- Origin: {history.coffee_origin}")
    if history.coffee_processing:
        lines.append(f"- Processing: {history.coffee_processing}")
    if history.coffee_altitude:
        lines.append(f"- Altitude: {history.coffee_altitude} MASL")
    if history.coffee_density_g_l is not None:
        lines.append(f"- Density: {history.coffee_density_g_l:g} g/L")
    if history.coffee_density:
        lines.append(f"- Density: {history.coffee_density}")
    if history.coffee_moisture is not None:
        lines.append(f"- Moisture: {history.coffee_moisture}%")
    return "\n".join(lines)
