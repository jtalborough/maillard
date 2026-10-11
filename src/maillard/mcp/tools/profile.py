"""MCP tools for roast profile management.

Provides tools to list, get, save, update, and delete roast profiles.
Also includes a stub for AI-generated profile creation (wired up in Phase 4).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from maillard.db.models import Profile
from maillard.db.repository import CoffeeRepository, ProfileRepository
from maillard.db.schemas import PhaseSchema, ProfileCreate, ProfileUpdate
from maillard.mcp.server import get_state_from_context, mcp

_logger = logging.getLogger(__name__)


def _profile_to_response(profile: Any) -> dict[str, Any]:
    """Convert a Profile document to a response dict.

    Phases are stored as native subdocuments (list of dicts).
    """
    return {
        "id": profile.id,
        "name": profile.name,
        "coffee_id": profile.coffee_id,
        "roast_level": profile.roast_level,
        "flavor_goal": profile.flavor_goal,
        "brew_method": profile.brew_method,
        "batch_size_grams": profile.batch_size_grams,
        "phases": profile.phases if profile.phases else [],
        "target_dtr": profile.target_dtr,
        "target_weight_loss": profile.target_weight_loss,
        "charge_temp": profile.charge_temp,
        "ai_rationale": profile.ai_rationale,
        "version": profile.version,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _profile_to_summary(profile: Any) -> dict[str, Any]:
    """Convert a Profile document to a summary dict."""
    return {
        "id": profile.id,
        "name": profile.name,
        "coffee_id": profile.coffee_id,
        "roast_level": profile.roast_level,
        "brew_method": profile.brew_method,
    }


# =============================================================================
# Profile Management Tools
# =============================================================================


@mcp.tool()
async def list_profiles(
    ctx: Any,
    coffee_id: str | None = None,
    roast_level: str | None = None,
    brew_method: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List saved roast profiles with optional filter.

    Retrieves a list of profiles from the database. You can filter by
    ONE of: coffee ID, roast level, or brew method. Only one filter
    can be applied at a time.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: Filter by associated coffee ID (mutually exclusive with other filters)
        roast_level: Filter by roast level - Light, Medium-Light, Medium, Medium-Dark, Dark
                     (mutually exclusive with other filters)
        brew_method: Filter by target brew method - Filter, Espresso, Both
                     (mutually exclusive with other filters)
        limit: Maximum number of results (default 50, max 100)
        offset: Number of results to skip for pagination

    Returns:
        Dict with profiles list, total count, limit, and offset

    Raises:
        ValueError: If more than one filter is provided
    """
    get_state_from_context(ctx)

    filters_provided = sum(
        1 for f in [coffee_id, roast_level, brew_method] if f is not None
    )
    if filters_provided > 1:
        raise ValueError(
            "Only one filter can be applied at a time. "
            "Provide only one of: coffee_id, roast_level, or brew_method."
        )

    limit = min(max(1, limit), 100)
    offset = max(0, offset)

    repo = ProfileRepository()
    profiles: Sequence[Profile]

    if coffee_id is not None:
        profiles = await repo.get_by_coffee_id(coffee_id, limit=limit, offset=offset)
        total = await repo.count(coffee_id=coffee_id)
    elif roast_level is not None:
        profiles = await repo.get_by_roast_level(roast_level, limit=limit, offset=offset)
        total = await repo.count(roast_level=roast_level)
    elif brew_method is not None:
        profiles = await repo.get_by_brew_method(brew_method, limit=limit, offset=offset)
        total = await repo.count(brew_method=brew_method)
    else:
        profiles = await repo.get_all(limit=limit, offset=offset)
        total = await repo.count()

    return {
        "profiles": [_profile_to_summary(p) for p in profiles],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@mcp.tool()
async def get_profile(ctx: Any, profile_id: str) -> dict[str, Any]:
    """Get full details of a roast profile including all phases.

    Retrieves all stored information about a profile by its ID,
    including the complete phase definitions for roast execution.

    Args:
        ctx: MCP context (injected automatically)
        profile_id: The UUID of the profile to retrieve

    Returns:
        Full profile details including phases array

    Raises:
        ValueError: If the profile is not found
    """
    get_state_from_context(ctx)

    repo = ProfileRepository()
    profile = await repo.get_by_id(profile_id)

    if profile is None:
        raise ValueError(f"Profile not found: {profile_id}")

    return _profile_to_response(profile)


@mcp.tool()
async def save_profile(
    ctx: Any,
    name: str,
    phases: list[dict[str, Any]],
    coffee_id: str | None = None,
    roast_level: str | None = None,
    flavor_goal: str | None = None,
    brew_method: str | None = None,
    batch_size_grams: int | None = None,
    target_dtr: float | None = None,
    target_weight_loss: float | None = None,
    charge_temp: float | None = None,
    ai_rationale: str | None = None,
) -> dict[str, Any]:
    """Save a new roast profile.

    Creates a new profile with the specified parameters. Each profile
    must have at least one phase defining temperature targets and
    heat/fan/drum settings.

    Phase format:
    {
        "name": "charge" | "turning_point" | "dry_end" | "maillard" | "fc_start" | "development" | "drop",
        "target_time_seconds": 0,     # Seconds from charge
        "target_temp": 195.0,         # Target temperature in Celsius
        "heat_percent": 100,          # Heating power 0-100%
        "fan_percent": 40,            # Fan speed 0-100%
        "drum_percent": 60,           # Drum speed 0-100%
        "target_ror": 15.0,           # Optional: target RoR in °C/min
        "notes": "Optional notes"     # Optional: phase notes
    }

    Args:
        ctx: MCP context (injected automatically)
        name: Name for the profile (e.g., "Light Roast for Filter")
        phases: List of phase definitions (at least one required)
        coffee_id: Optional associated coffee ID
        roast_level: Roast level - Light, Medium-Light, Medium, Medium-Dark, Dark
        flavor_goal: Desired flavor characteristics
        brew_method: Target brew method - Filter, Espresso, Both
        batch_size_grams: Batch size this profile is designed for
        target_dtr: Target Development Time Ratio percentage (0-100)
        target_weight_loss: Target weight loss percentage
        charge_temp: Charge temperature in Celsius
        ai_rationale: AI explanation for profile choices (set by generate_profile)

    Returns:
        The created profile record with ID and timestamps

    Raises:
        ValueError: If phases list is empty or contains invalid data
    """
    get_state_from_context(ctx)

    if not phases:
        raise ValueError("At least one phase is required")

    try:
        validated_phases = [PhaseSchema(**phase) for phase in phases]
    except Exception as e:
        raise ValueError(f"Invalid phase data: {e}") from e

    profile_data = ProfileCreate(
        name=name,
        phases=validated_phases,
        coffee_id=coffee_id,
        roast_level=roast_level,
        flavor_goal=flavor_goal,
        brew_method=brew_method,
        batch_size_grams=batch_size_grams,
        target_dtr=target_dtr,
        target_weight_loss=target_weight_loss,
        charge_temp=charge_temp,
        ai_rationale=ai_rationale,
    )

    repo = ProfileRepository()
    profile = await repo.create(profile_data)

    _logger.info("Created profile %s: %s", profile.id, profile.name)
    return _profile_to_response(profile)


@mcp.tool()
async def update_profile(
    ctx: Any,
    profile_id: str,
    name: str | None = None,
    phases: list[dict[str, Any]] | None = None,
    coffee_id: str | None = None,
    roast_level: str | None = None,
    flavor_goal: str | None = None,
    brew_method: str | None = None,
    batch_size_grams: int | None = None,
    target_dtr: float | None = None,
    target_weight_loss: float | None = None,
    charge_temp: float | None = None,
    ai_rationale: str | None = None,
) -> dict[str, Any]:
    """Update an existing roast profile.

    Updates the specified fields for a profile. Only provide the fields
    you want to change - unspecified fields will not be modified.

    Note: Updating a profile increments its version number.

    Args:
        ctx: MCP context (injected automatically)
        profile_id: The UUID of the profile to update
        name: New name for the profile
        phases: New phase definitions (replaces existing phases)
        coffee_id: New associated coffee ID
        roast_level: New roast level
        flavor_goal: New flavor goal
        brew_method: New brew method
        batch_size_grams: New batch size
        target_dtr: New target DTR
        target_weight_loss: New target weight loss
        charge_temp: New charge temperature
        ai_rationale: New AI rationale

    Returns:
        The updated profile record with incremented version

    Raises:
        ValueError: If the profile is not found or no fields provided
    """
    get_state_from_context(ctx)

    update_kwargs: dict[str, Any] = {}

    if name is not None:
        update_kwargs["name"] = name
    if coffee_id is not None:
        update_kwargs["coffee_id"] = coffee_id
    if roast_level is not None:
        update_kwargs["roast_level"] = roast_level
    if flavor_goal is not None:
        update_kwargs["flavor_goal"] = flavor_goal
    if brew_method is not None:
        update_kwargs["brew_method"] = brew_method
    if batch_size_grams is not None:
        update_kwargs["batch_size_grams"] = batch_size_grams
    if target_dtr is not None:
        update_kwargs["target_dtr"] = target_dtr
    if target_weight_loss is not None:
        update_kwargs["target_weight_loss"] = target_weight_loss
    if charge_temp is not None:
        update_kwargs["charge_temp"] = charge_temp
    if ai_rationale is not None:
        update_kwargs["ai_rationale"] = ai_rationale

    if phases is not None:
        try:
            validated_phases = [PhaseSchema(**phase) for phase in phases]
            update_kwargs["phases"] = validated_phases
        except Exception as e:
            raise ValueError(f"Invalid phase data: {e}") from e

    if not update_kwargs:
        raise ValueError("No fields provided to update")

    update_data = ProfileUpdate(**update_kwargs)

    repo = ProfileRepository()
    profile = await repo.update(profile_id, update_data)

    if profile is None:
        raise ValueError(f"Profile not found: {profile_id}")

    _logger.info("Updated profile %s (version %d)", profile_id, profile.version)
    return _profile_to_response(profile)


@mcp.tool()
async def delete_profile(ctx: Any, profile_id: str) -> dict[str, Any]:
    """Remove a roast profile.

    Permanently deletes a profile. This action cannot be undone.
    Roasts that used this profile will have their profile_id set to NULL.

    Args:
        ctx: MCP context (injected automatically)
        profile_id: The UUID of the profile to delete

    Returns:
        Confirmation dict with deleted profile_id

    Raises:
        ValueError: If the profile is not found
    """
    get_state_from_context(ctx)

    repo = ProfileRepository()
    deleted = await repo.delete(profile_id)

    if not deleted:
        raise ValueError(f"Profile not found: {profile_id}")

    _logger.info("Deleted profile %s", profile_id)
    return {"deleted": True, "profile_id": profile_id}


@mcp.tool()
async def generate_profile(
    ctx: Any,
    coffee_id: str,
    roast_level: str,
    flavor_goal: str,
    brew_method: str,
    batch_size_grams: int,
) -> dict[str, Any]:
    """Generate an AI-recommended roast profile based on coffee characteristics.

    Uses coffee origin, processing method, altitude, density, and past roast
    history to create an optimized 7-phase roast profile with appropriate
    timings, temperatures, and heat/fan/drum settings.

    In MCP context, generates heuristic profiles only (no Anthropic API calls).
    The web UI endpoint provides AI-enhanced profiles when an API key is available.

    Args:
        ctx: MCP context (injected automatically)
        coffee_id: The coffee to create a profile for
        roast_level: Target roast level - Light, Medium-Light, Medium, Medium-Dark, Dark
        flavor_goal: Desired flavor characteristics (e.g., "highlight floral and citrus notes")
        brew_method: Target brew method - Filter, Espresso, Both
        batch_size_grams: Batch size for the profile

    Returns:
        dict: Generated profile with phases, charge_temp, target_dtr, ai_rationale, and source

    Raises:
        ValueError: If the coffee is not found
    """
    get_state_from_context(ctx)

    coffee_repo = CoffeeRepository()
    coffee = await coffee_repo.get_by_id(coffee_id)

    if coffee is None:
        raise ValueError(f"Coffee not found: {coffee_id}")

    from maillard.ai.generate import generate_profile as _generate

    result = await _generate(
        coffee_id=coffee_id,
        roast_level=roast_level,
        flavor_goal=flavor_goal,
        brew_method=brew_method,
        batch_size_grams=batch_size_grams,
    )

    return {
        "name": result.name,
        "phases": result.phases,
        "charge_temp": result.charge_temp,
        "target_dtr": result.target_dtr,
        "ai_rationale": result.ai_rationale,
        "source": result.source,
        "roast_level": result.roast_level,
        "brew_method": result.brew_method,
        "flavor_goal": result.flavor_goal,
        "batch_size_grams": result.batch_size_grams,
    }


__all__ = [
    "delete_profile",
    "generate_profile",
    "get_profile",
    "list_profiles",
    "save_profile",
    "update_profile",
]
