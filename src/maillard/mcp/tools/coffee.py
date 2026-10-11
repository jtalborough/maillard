"""MCP tools for coffee inventory management.

Provides tools to add, list, get, update, and delete green coffee entries.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from maillard.db.models import Coffee
from maillard.db.repository import CoffeeRepository
from maillard.db.schemas import CoffeeCreate, CoffeeUpdate
from maillard.mcp.server import get_state_from_context, mcp

_logger = logging.getLogger(__name__)


async def _coffee_to_response(coffee: Any) -> dict[str, Any]:
    """Convert a Coffee document to a response dict."""
    from maillard.inventory.service import coffee_summary
    balance = await coffee_summary(coffee.id)
    return {
        "id": coffee.id,
        "name": coffee.name,
        "origin": coffee.origin,
        "processing": coffee.processing,
        "varietal": coffee.varietal,
        "altitude": coffee.altitude,
        "altitude_category": coffee.altitude_category,
        "moisture": coffee.moisture,
        "density": coffee.density,
        "harvest_date": coffee.harvest_date,
        "purchase_date": coffee.purchase_date,
        "quantity_grams": balance["remaining"],
        "inventory_known": balance["known"],
        "notes": coffee.notes,
        "created_at": coffee.created_at,
        "updated_at": coffee.updated_at,
    }


def _coffee_to_summary(coffee: Any) -> dict[str, Any]:
    """Convert a Coffee document to a summary dict."""
    return {
        "id": coffee.id,
        "name": coffee.name,
        "origin": coffee.origin,
        "processing": coffee.processing,
    }


# =============================================================================
# Coffee Management Tools
# =============================================================================


@mcp.tool()
async def add_coffee(
    ctx: Any,
    name: str,
    origin: str,
    processing: str | None = None,
    varietal: list[str] | None = None,
    altitude: int | None = None,
    altitude_category: str | None = None,
    moisture: float | None = None,
    density: str | None = None,
    harvest_date: str | None = None,
    purchase_date: str | None = None,
    quantity_grams: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Register a new green coffee in the inventory.

    Use this tool to add a new coffee bean to track. You can record
    origin details, processing method, and physical characteristics
    that help with roast profile creation.

    Args:
        ctx: MCP context (injected automatically)
        name: Name/label for the coffee (e.g., "Ethiopia Yirgacheffe")
        origin: Country or region of origin (e.g., "Ethiopia", "Colombia Huila")
        processing: Processing method - Washed, Natural, Honey, or Anaerobic
        varietal: List of varietals (e.g., ["Heirloom", "SL28"])
        altitude: Altitude in meters above sea level (MASL)
        altitude_category: Altitude category if MASL unknown - Low, Medium, or High
        moisture: Moisture percentage (typically 10-12%)
        density: Density category - Low, Medium, or High
        harvest_date: When the coffee was harvested (ISO date format YYYY-MM-DD)
        purchase_date: When you purchased the coffee (ISO date format YYYY-MM-DD)
        quantity_grams: Amount of green coffee in grams
        notes: Additional notes about the coffee

    Returns:
        The created coffee record with ID and timestamps
    """
    get_state_from_context(ctx)  # validate state is initialized

    coffee_data = CoffeeCreate(
        name=name,
        origin=origin,
        processing=processing,
        varietal=varietal,
        altitude=altitude,
        altitude_category=altitude_category,
        moisture=moisture,
        density=density,
        harvest_date=harvest_date,
        purchase_date=purchase_date,
        quantity_grams=quantity_grams,
        notes=notes,
    )

    repo = CoffeeRepository()
    coffee = await repo.create(coffee_data)

    _logger.info("Created coffee %s: %s", coffee.id, coffee.name)
    return await _coffee_to_response(coffee)


@mcp.tool()
async def list_coffees(
    ctx: Any,
    origin_filter: str | None = None,
    processing_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List all registered coffees with optional filters.

    Retrieves a list of coffees from the inventory. You can filter by
    origin and/or processing method to find specific beans. Both filters
    can be combined for more specific searches.

    Args:
        ctx: MCP context (injected automatically)
        origin_filter: Filter by origin (partial match, case-insensitive)
        processing_filter: Filter by processing method (partial match, case-insensitive)
        limit: Maximum number of results (default 50, max 100)
        offset: Number of results to skip for pagination

    Returns:
        Dict with coffees list, total count matching filters, limit, and offset
    """
    get_state_from_context(ctx)

    limit = min(max(1, limit), 100)
    offset = max(0, offset)

    repo = CoffeeRepository()
    coffees: Sequence[Coffee]

    if origin_filter is not None and processing_filter is not None:
        coffees_by_origin = await repo.get_by_origin(
            origin_filter, limit=limit + offset, offset=0
        )
        filtered = [
            c for c in coffees_by_origin
            if c.processing and processing_filter.lower() in c.processing.lower()
        ]
        total = len(filtered)
        coffees = filtered[offset : offset + limit]
    elif origin_filter is not None:
        coffees = await repo.get_by_origin(origin_filter, limit=limit, offset=offset)
        total = await repo.count(origin=origin_filter)
    elif processing_filter is not None:
        coffees = await repo.get_by_processing(
            processing_filter, limit=limit, offset=offset
        )
        total = await repo.count(processing=processing_filter)
    else:
        coffees = await repo.get_all(limit=limit, offset=offset)
        total = await repo.count()

    return {
        "coffees": [_coffee_to_summary(c) for c in coffees],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@mcp.tool()
async def get_coffee(ctx: Any, coffee_id: str) -> dict[str, Any]:
    """Get detailed information about a specific coffee.

    Retrieves all stored information about a coffee by its ID.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: The UUID of the coffee to retrieve

    Returns:
        Full coffee details including all characteristics

    Raises:
        ValueError: If the coffee is not found
    """
    get_state_from_context(ctx)

    repo = CoffeeRepository()
    coffee = await repo.get_by_id(coffee_id)

    if coffee is None:
        raise ValueError(f"Coffee not found: {coffee_id}")

    return await _coffee_to_response(coffee)


@mcp.tool()
async def update_coffee(
    ctx: Any,
    coffee_id: str,
    name: str | None = None,
    origin: str | None = None,
    processing: str | None = None,
    varietal: list[str] | None = None,
    altitude: int | None = None,
    altitude_category: str | None = None,
    moisture: float | None = None,
    density: str | None = None,
    harvest_date: str | None = None,
    purchase_date: str | None = None,
    quantity_grams: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Update information about an existing coffee.

    Updates the specified fields for a coffee. Only provide the fields
    you want to change - unspecified fields will not be modified.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: The UUID of the coffee to update
        name: New name for the coffee
        origin: New origin
        processing: New processing method
        varietal: New varietal list
        altitude: New altitude in MASL
        altitude_category: New altitude category
        moisture: New moisture percentage
        density: New density category
        harvest_date: New harvest date
        purchase_date: New purchase date
        quantity_grams: New quantity
        notes: New notes

    Returns:
        The updated coffee record

    Raises:
        ValueError: If the coffee is not found
    """
    get_state_from_context(ctx)

    update_kwargs: dict[str, Any] = {}
    if name is not None:
        update_kwargs["name"] = name
    if origin is not None:
        update_kwargs["origin"] = origin
    if processing is not None:
        update_kwargs["processing"] = processing
    if varietal is not None:
        update_kwargs["varietal"] = varietal
    if altitude is not None:
        update_kwargs["altitude"] = altitude
    if altitude_category is not None:
        update_kwargs["altitude_category"] = altitude_category
    if moisture is not None:
        update_kwargs["moisture"] = moisture
    if density is not None:
        update_kwargs["density"] = density
    if harvest_date is not None:
        update_kwargs["harvest_date"] = harvest_date
    if purchase_date is not None:
        update_kwargs["purchase_date"] = purchase_date
    if quantity_grams is not None:
        update_kwargs["quantity_grams"] = quantity_grams
    if notes is not None:
        update_kwargs["notes"] = notes

    if not update_kwargs:
        raise ValueError("No fields provided to update")

    update_data = CoffeeUpdate(**update_kwargs)

    repo = CoffeeRepository()
    coffee = await repo.update(coffee_id, update_data)

    if coffee is None:
        raise ValueError(f"Coffee not found: {coffee_id}")

    _logger.info("Updated coffee %s", coffee_id)
    return await _coffee_to_response(coffee)


@mcp.tool()
async def delete_coffee(ctx: Any, coffee_id: str) -> dict[str, Any]:
    """Remove a coffee from inventory.

    Permanently deletes a coffee record along with all associated profiles
    and roasts (via ORM cascade). This action cannot be undone.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: The UUID of the coffee to delete

    Returns:
        Confirmation dict with deleted coffee_id

    Raises:
        ValueError: If the coffee is not found
    """
    get_state_from_context(ctx)

    repo = CoffeeRepository()
    deleted = await repo.delete(coffee_id)

    if not deleted:
        raise ValueError(f"Coffee not found: {coffee_id}")

    _logger.info("Deleted coffee %s", coffee_id)
    return {"deleted": True, "coffee_id": coffee_id}


__all__ = [
    "add_coffee",
    "delete_coffee",
    "get_coffee",
    "list_coffees",
    "update_coffee",
]


@mcp.tool()
async def get_inventory_summary(ctx: Any) -> dict[str, Any]:
    """Read remaining, reserved, free green, and lots per coffee; no inventory edits."""
    get_state_from_context(ctx)
    from maillard.inventory.service import inventory_summary
    return {"coffees": await inventory_summary()}
