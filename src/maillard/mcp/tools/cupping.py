"""MCP tools for cupping record management.

Provides tools to add, list, get, update, and delete cupping entries
for roasted coffee evaluations.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from maillard.db.models import Cupping
from maillard.db.repository import CoffeeRepository, CuppingRepository, RoastRepository
from maillard.db.schemas import CuppingCreate, CuppingScores, CuppingUpdate
from maillard.mcp.server import get_state_from_context, mcp

_logger = logging.getLogger(__name__)


def _calculate_total_score(cupping: Any) -> float | None:
    """Calculate total score from 6 SCA scores.

    Args:
        cupping: Cupping document instance

    Returns:
        Sum of all non-None scores, or None if no scores present
    """
    scores = [
        cupping.aroma,
        cupping.acidity,
        cupping.sweetness,
        cupping.body,
        cupping.aftertaste,
        cupping.overall,
    ]
    non_none_scores = [s for s in scores if s is not None]
    if not non_none_scores:
        return None
    return sum(non_none_scores)


def _cupping_to_response(cupping: Any) -> dict[str, Any]:
    """Convert a Cupping document to a response dict.

    Args:
        cupping: Cupping document instance

    Returns:
        Dict representation suitable for MCP response
    """
    return {
        "id": cupping.id,
        "roast_id": cupping.roast_id,
        "rest_days": cupping.rest_days,
        "aroma": cupping.aroma,
        "acidity": cupping.acidity,
        "sweetness": cupping.sweetness,
        "body": cupping.body,
        "aftertaste": cupping.aftertaste,
        "overall": cupping.overall,
        "total_score": _calculate_total_score(cupping),
        "flavor_notes": cupping.flavor_notes,
        "defects": cupping.defects,
        "brew_method": cupping.brew_method,
        "cupper_name": cupping.cupper_name,
        "notes": cupping.notes,
        "cupped_at": cupping.cupped_at,
    }


def _cupping_to_summary(cupping: Any) -> dict[str, Any]:
    """Convert a Cupping document to a summary dict.

    Args:
        cupping: Cupping document instance

    Returns:
        Dict with id, roast_id, total_score, cupped_at
    """
    return {
        "id": cupping.id,
        "roast_id": cupping.roast_id,
        "total_score": _calculate_total_score(cupping),
        "cupped_at": cupping.cupped_at,
    }


# =============================================================================
# Cupping Management Tools
# =============================================================================


@mcp.tool()
async def add_cupping(
    ctx: Any,
    roast_id: str,
    aroma: float | None = None,
    acidity: float | None = None,
    sweetness: float | None = None,
    body: float | None = None,
    aftertaste: float | None = None,
    overall: float | None = None,
    flavor_notes: list[str] | None = None,
    defects: list[str] | None = None,
    rest_days: int | None = None,
    brew_method: str | None = None,
    cupper_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record cupping scores and notes for a roast.

    Use this tool to capture tasting evaluation data after cupping a roasted
    coffee. Scores follow the SCA standard (1-10 scale for each attribute).
    The total score is calculated automatically as the sum of all scores (max 60).

    Args:
        ctx: MCP context (injected automatically)
        roast_id: UUID of the roast being evaluated (must exist in database)
        aroma: Aroma score (1-10)
        acidity: Acidity score (1-10)
        sweetness: Sweetness score (1-10)
        body: Body score (1-10)
        aftertaste: Aftertaste score (1-10)
        overall: Overall impression score (1-10)
        flavor_notes: List of flavor descriptors (e.g., ["citrus", "floral"])
        defects: List of defect notes (e.g., ["baked", "grassy"])
        rest_days: Number of days since roast
        brew_method: How the coffee was brewed (e.g., "V60", "Cupping bowl")
        cupper_name: Name of the person cupping (e.g., "John")
        notes: Additional tasting notes

    Returns:
        The created cupping record with ID, scores, and calculated total

    Raises:
        ValueError: If the roast_id does not exist in the database
    """
    get_state_from_context(ctx)

    # Validate roast exists
    roast_repo = RoastRepository()
    roast = await roast_repo.get_by_id(roast_id)
    if roast is None:
        raise ValueError(f"Roast not found: {roast_id}")

    # Build scores if any provided
    scores = None
    if any(
        s is not None
        for s in [aroma, acidity, sweetness, body, aftertaste, overall]
    ):
        scores = CuppingScores(
            aroma=aroma,
            acidity=acidity,
            sweetness=sweetness,
            body=body,
            aftertaste=aftertaste,
            overall=overall,
        )

    # Create Pydantic schema for validation
    cupping_data = CuppingCreate(
        roast_id=roast_id,
        scores=scores,
        flavor_notes=flavor_notes,
        defects=defects,
        rest_days=rest_days,
        brew_method=brew_method,
        cupper_name=cupper_name,
        notes=notes,
    )

    repo = CuppingRepository()
    cupping = await repo.create(cupping_data)

    _logger.info("Created cupping %s for roast %s", cupping.id, roast_id)
    return _cupping_to_response(cupping)


@mcp.tool()
async def list_cuppings(
    ctx: Any,
    roast_id: str | None = None,
    coffee_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List cupping records filtered by roast or coffee.

    Retrieves cupping records with optional filtering. You can filter by
    roast_id to see all cuppings for a specific roast, or by coffee_id
    to see all cuppings across all roasts of a specific coffee.

    Args:
        ctx: MCP context (injected automatically)
        roast_id: Filter by specific roast UUID
        coffee_id: Filter by coffee UUID (returns cuppings for all roasts of this coffee)
        limit: Maximum number of results (default 50, max 100)
        offset: Number of results to skip for pagination

    Returns:
        Dict with cuppings list (summaries), total count, limit, and offset
    """
    get_state_from_context(ctx)

    # Clamp limit to reasonable bounds
    limit = min(max(1, limit), 100)
    offset = max(0, offset)

    repo = CuppingRepository()
    cuppings: Sequence[Cupping]

    if roast_id is not None:
        cuppings = await repo.get_by_roast_id(roast_id, limit=limit, offset=offset)
        total = await repo.count(roast_id=roast_id)

    elif coffee_id is not None:
        # Two-query pattern: get cuppings via roast IDs
        cuppings = await repo.get_by_coffee_id(coffee_id, limit=limit, offset=offset)
        # Count total for this coffee (all cuppings, not just this page)
        all_cuppings = await repo.get_by_coffee_id(coffee_id, limit=10000, offset=0)
        total = len(all_cuppings)

    else:
        cuppings = await repo.get_all(limit=limit, offset=offset)
        total = await repo.count()

    return {
        "cuppings": [_cupping_to_summary(c) for c in cuppings],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@mcp.tool()
async def get_cupping(ctx: Any, cupping_id: str) -> dict[str, Any]:
    """Get full details for a cupping entry.

    Retrieves all stored information about a cupping evaluation by its ID,
    including all scores, flavor notes, defects, and calculated total.

    Args:
        ctx: MCP context (injected automatically)
        cupping_id: The UUID of the cupping to retrieve

    Returns:
        Full cupping details including all scores and notes

    Raises:
        ValueError: If the cupping is not found
    """
    get_state_from_context(ctx)

    repo = CuppingRepository()
    cupping = await repo.get_by_id(cupping_id)

    if cupping is None:
        raise ValueError(f"Cupping not found: {cupping_id}")

    return _cupping_to_response(cupping)


@mcp.tool()
async def update_cupping(
    ctx: Any,
    cupping_id: str,
    aroma: float | None = None,
    acidity: float | None = None,
    sweetness: float | None = None,
    body: float | None = None,
    aftertaste: float | None = None,
    overall: float | None = None,
    flavor_notes: list[str] | None = None,
    defects: list[str] | None = None,
    rest_days: int | None = None,
    brew_method: str | None = None,
    cupper_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Update scores and/or notes on an existing cupping.

    Updates the specified fields for a cupping entry. Only provide the fields
    you want to change - unspecified fields will not be modified.

    Args:
        ctx: MCP context (injected automatically)
        cupping_id: The UUID of the cupping to update
        aroma: New aroma score (1-10)
        acidity: New acidity score (1-10)
        sweetness: New sweetness score (1-10)
        body: New body score (1-10)
        aftertaste: New aftertaste score (1-10)
        overall: New overall score (1-10)
        flavor_notes: New flavor descriptors list
        defects: New defect notes list
        rest_days: New rest days value
        brew_method: New brew method
        cupper_name: New cupper name
        notes: New tasting notes

    Returns:
        The updated cupping record with recalculated total

    Raises:
        ValueError: If the cupping is not found or no fields provided
    """
    get_state_from_context(ctx)

    # Build update data
    has_scores = any(
        s is not None for s in [aroma, acidity, sweetness, body, aftertaste, overall]
    )
    has_other_fields = any(
        f is not None for f in [flavor_notes, defects, rest_days, brew_method, cupper_name, notes]
    )

    if not has_scores and not has_other_fields:
        raise ValueError("No fields provided to update")

    # Build scores if any score fields provided
    scores = None
    if has_scores:
        score_kwargs: dict[str, float] = {}
        if aroma is not None:
            score_kwargs["aroma"] = aroma
        if acidity is not None:
            score_kwargs["acidity"] = acidity
        if sweetness is not None:
            score_kwargs["sweetness"] = sweetness
        if body is not None:
            score_kwargs["body"] = body
        if aftertaste is not None:
            score_kwargs["aftertaste"] = aftertaste
        if overall is not None:
            score_kwargs["overall"] = overall
        scores = CuppingScores(**score_kwargs)

    # Build update kwargs, excluding None values
    update_kwargs: dict[str, Any] = {}
    if scores is not None:
        update_kwargs["scores"] = scores
    if flavor_notes is not None:
        update_kwargs["flavor_notes"] = flavor_notes
    if defects is not None:
        update_kwargs["defects"] = defects
    if rest_days is not None:
        update_kwargs["rest_days"] = rest_days
    if brew_method is not None:
        update_kwargs["brew_method"] = brew_method
    if cupper_name is not None:
        update_kwargs["cupper_name"] = cupper_name
    if notes is not None:
        update_kwargs["notes"] = notes

    update_data = CuppingUpdate(**update_kwargs)

    repo = CuppingRepository()
    cupping = await repo.update(cupping_id, update_data)

    if cupping is None:
        raise ValueError(f"Cupping not found: {cupping_id}")

    _logger.info("Updated cupping %s", cupping_id)
    return _cupping_to_response(cupping)


@mcp.tool()
async def delete_cupping(ctx: Any, cupping_id: str) -> dict[str, Any]:
    """Remove a cupping entry.

    Permanently deletes a cupping record. This action cannot be undone.

    Args:
        ctx: MCP context (injected automatically)
        cupping_id: The UUID of the cupping to delete

    Returns:
        Confirmation dict with deleted cupping_id

    Raises:
        ValueError: If the cupping is not found
    """
    get_state_from_context(ctx)

    repo = CuppingRepository()
    deleted = await repo.delete(cupping_id)

    if not deleted:
        raise ValueError(f"Cupping not found: {cupping_id}")

    _logger.info("Deleted cupping %s", cupping_id)
    return {"deleted": True, "cupping_id": cupping_id}


# =============================================================================
# Cupping Aggregation Tools
# =============================================================================


def _aggregation_to_dict(agg: Any) -> dict[str, Any]:
    """Convert a CuppingAggregation dataclass to a JSON-serializable dict."""
    return {
        "count": agg.count,
        "avg_total": agg.avg_total,
        "dimension_averages": [
            {"dimension": d.dimension, "average": d.average, "count": d.count}
            for d in agg.dimension_averages
        ],
        "ranked_flavor_notes": agg.ranked_flavor_notes,
        "ranked_defects": agg.ranked_defects,
    }


@mcp.tool()
async def get_cupping_aggregation(
    ctx: Any,
    roast_id: str,
) -> dict[str, Any]:
    """Get aggregated cupping scores for a roast.

    Computes per-dimension averages, mean total score, and frequency-ranked
    flavor notes across all cuppings for the specified roast. Useful when a
    roast has multiple cuppings (different rest days, different tasters).

    Args:
        ctx: MCP context (injected automatically)
        roast_id: UUID of the roast to aggregate cuppings for

    Returns:
        Dict with count, avg_total, dimension_averages, ranked_flavor_notes,
        and ranked_defects

    Raises:
        ValueError: If the roast is not found
    """
    from maillard.db.aggregation import aggregate_cuppings

    get_state_from_context(ctx)

    roast_repo = RoastRepository()
    roast = await roast_repo.get_by_id(roast_id)
    if roast is None:
        raise ValueError(f"Roast not found: {roast_id}")

    # Load cuppings separately (no ORM relationship loading)
    cupping_repo = CuppingRepository()
    cuppings = await cupping_repo.get_by_roast_id(roast_id)

    agg = aggregate_cuppings(cuppings)
    result = _aggregation_to_dict(agg)
    result["roast_id"] = roast_id
    return result


@mcp.tool()
async def get_coffee_cupping_summary(
    ctx: Any,
    coffee_id: str,
) -> dict[str, Any]:
    """Get cupping summary across all roasts of a coffee.

    Loads all completed roasts for the coffee, computes per-roast and overall
    cupping aggregation. Useful for understanding flavor trends across roasts.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: UUID of the coffee

    Returns:
        Dict with overall aggregation and per-roast breakdown

    Raises:
        ValueError: If the coffee is not found
    """
    from maillard.db.aggregation import aggregate_cuppings

    get_state_from_context(ctx)

    coffee_repo = CoffeeRepository()
    coffee = await coffee_repo.get_by_id(coffee_id)
    if coffee is None:
        raise ValueError(f"Coffee not found: {coffee_id}")

    roast_repo = RoastRepository()
    roasts = await roast_repo.get_completed_by_coffee_id(coffee_id, limit=50)

    # Load cuppings for each roast separately
    cupping_repo = CuppingRepository()
    all_cuppings: list[Any] = []
    per_roast = []
    for r in roasts:
        roast_cuppings = await cupping_repo.get_by_roast_id(r.id)
        all_cuppings.extend(roast_cuppings)
        roast_agg = aggregate_cuppings(roast_cuppings)
        per_roast.append({
            "roast_id": r.id,
            "date": r.start_time[:10] if r.start_time else None,
            **_aggregation_to_dict(roast_agg),
        })

    overall = aggregate_cuppings(all_cuppings)

    return {
        "coffee_id": coffee_id,
        "coffee_name": coffee.name,
        "overall": _aggregation_to_dict(overall),
        "per_roast": per_roast,
    }


__all__ = [
    "add_cupping",
    "delete_cupping",
    "get_coffee_cupping_summary",
    "get_cupping",
    "get_cupping_aggregation",
    "list_cuppings",
    "update_cupping",
]
