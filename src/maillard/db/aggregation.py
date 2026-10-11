"""Cupping score aggregation functions.

Pure functions that aggregate cupping scores across multiple cuppings
for a roast. Used by MCP tools, AI history, web routes, and comparison
endpoints to provide consistent aggregated data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


SCORE_DIMENSIONS = ("aroma", "acidity", "sweetness", "body", "aftertaste", "overall")


@dataclass
class DimensionAverage:
    """Average score for a single cupping dimension."""

    dimension: str
    average: float
    count: int  # number of non-None values that contributed


@dataclass
class CuppingAggregation:
    """Aggregated cupping data across multiple cuppings for a roast."""

    count: int  # total number of cuppings
    avg_total: float | None  # mean of per-cupping totals (None if no scores)
    dimension_averages: list[DimensionAverage] = field(default_factory=list)
    ranked_flavor_notes: list[str] = field(default_factory=list)
    ranked_defects: list[str] = field(default_factory=list)


def _cupping_total(cupping: Any) -> float | None:
    """Calculate total score for a single cupping (sum of non-None scores)."""
    scores = [getattr(cupping, dim, None) for dim in SCORE_DIMENSIONS]
    non_none = [s for s in scores if s is not None]
    if not non_none:
        return None
    return sum(non_none)


def dimension_averages(cuppings: Sequence[Any]) -> list[DimensionAverage]:
    """Compute per-dimension averages across cuppings, skipping None values."""
    results = []
    for dim in SCORE_DIMENSIONS:
        values = [getattr(c, dim) for c in cuppings if getattr(c, dim, None) is not None]
        if values:
            results.append(DimensionAverage(
                dimension=dim,
                average=round(sum(values) / len(values), 2),
                count=len(values),
            ))
    return results


def ranked_notes(cuppings: Sequence[Any], attr: str = "flavor_notes") -> list[str]:
    """Rank flavor notes (or defects) by frequency, alphabetical tiebreak.

    With MongoDB, the attribute is a native list — no JSON parsing needed.
    """
    counter: Counter[str] = Counter()
    for c in cuppings:
        notes_list = getattr(c, attr, None)
        if isinstance(notes_list, list):
            for note in notes_list:
                if isinstance(note, str) and note.strip():
                    counter[note.strip().lower()] += 1
    # Sort by frequency desc, then alphabetical asc
    return [note for note, _ in sorted(counter.items(), key=lambda x: (-x[1], x[0]))]


def aggregate_cuppings(cuppings: Sequence[Any]) -> CuppingAggregation:
    """Aggregate cupping scores across multiple cuppings.

    Args:
        cuppings: Sequence of Cupping document instances.

    Returns:
        CuppingAggregation with count, avg_total, dimension averages,
        and frequency-ranked flavor notes/defects.
    """
    if not cuppings:
        return CuppingAggregation(count=0, avg_total=None)

    # Per-cupping totals
    totals = [_cupping_total(c) for c in cuppings]
    non_none_totals = [t for t in totals if t is not None]
    avg_total = round(sum(non_none_totals) / len(non_none_totals), 2) if non_none_totals else None

    return CuppingAggregation(
        count=len(cuppings),
        avg_total=avg_total,
        dimension_averages=dimension_averages(cuppings),
        ranked_flavor_notes=ranked_notes(cuppings, "flavor_notes"),
        ranked_defects=ranked_notes(cuppings, "defects"),
    )


__all__ = [
    "CuppingAggregation",
    "DimensionAverage",
    "aggregate_cuppings",
    "dimension_averages",
    "ranked_notes",
]
