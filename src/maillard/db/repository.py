"""Repository layer for Maillard data access.

Provides async CRUD operations for Coffee, Profile, Roast, and Cupping entities
using Beanie ODM. Repositories are stateless — Beanie uses global state after
init_beanie() is called.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from maillard.db.models import Coffee, Cupping, Profile, Roast, RoastStatus, utc_now
from maillard.db.schemas import (
    CoffeeCreate,
    CoffeeUpdate,
    CuppingCreate,
    CuppingUpdate,
    CurveDataSchema,
    ProfileCreate,
    ProfileUpdate,
    RoastCreate,
    RoastUpdate,
)

# =============================================================================
# Coffee Repository
# =============================================================================


class CoffeeRepository:
    """Repository for Coffee entity CRUD operations."""

    async def create(self, data: CoffeeCreate) -> Coffee:
        """Create a new coffee entry."""
        coffee = Coffee(**data.model_dump())
        await coffee.insert()
        return coffee

    async def get_by_id(self, coffee_id: str) -> Coffee | None:
        """Get a coffee by its ID."""
        return await Coffee.get(coffee_id)

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Coffee]:
        """Get all coffees with pagination."""
        return await Coffee.find().sort("-created_at").skip(offset).limit(limit).to_list()

    async def get_by_origin(
        self,
        origin: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Coffee]:
        """Get coffees filtered by origin (case-insensitive partial match)."""
        pattern = re.compile(re.escape(origin), re.IGNORECASE)
        return (
            await Coffee.find({"origin": {"$regex": pattern}})
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_by_processing(
        self,
        processing: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Coffee]:
        """Get coffees filtered by processing method (case-insensitive partial match)."""
        pattern = re.compile(re.escape(processing), re.IGNORECASE)
        return (
            await Coffee.find({"processing": {"$regex": pattern}})
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def update(self, coffee_id: str, data: CoffeeUpdate) -> Coffee | None:
        """Update a coffee entry."""
        coffee = await self.get_by_id(coffee_id)
        if coffee is None:
            return None

        update_data = data.model_dump(exclude_unset=True)
        update_data["updated_at"] = utc_now()

        for field_name, value in update_data.items():
            setattr(coffee, field_name, value)

        await coffee.save()
        return coffee

    async def delete(self, coffee_id: str) -> bool:
        """Delete a coffee and cascade to profiles, roasts, and their cuppings."""
        coffee = await self.get_by_id(coffee_id)
        if coffee is None:
            return False

        # CASCADE: delete all cuppings for roasts of this coffee
        roasts = await Roast.find(Roast.coffee_id == coffee_id).to_list()
        roast_ids = [r.id for r in roasts]
        if roast_ids:
            await Cupping.find({"roast_id": {"$in": roast_ids}}).delete()

        # CASCADE: delete all roasts for this coffee
        await Roast.find(Roast.coffee_id == coffee_id).delete()

        # CASCADE: delete all profiles for this coffee
        await Profile.find(Profile.coffee_id == coffee_id).delete()

        await coffee.delete()
        return True

    async def count(
        self,
        origin: str | None = None,
        processing: str | None = None,
    ) -> int:
        """Get total count of coffees, optionally filtered."""
        filters: dict = {}  # type: ignore[type-arg]
        if origin is not None:
            filters["origin"] = {"$regex": re.compile(re.escape(origin), re.IGNORECASE)}
        if processing is not None:
            filters["processing"] = {
                "$regex": re.compile(re.escape(processing), re.IGNORECASE)
            }
        return await Coffee.find(filters).count()


# =============================================================================
# Profile Repository
# =============================================================================


class ProfileRepository:
    """Repository for Profile entity CRUD operations."""

    async def create(self, data: ProfileCreate) -> Profile:
        """Create a new profile."""
        profile_data = data.model_dump()
        # Convert PhaseSchema objects to dicts for native subdocument storage
        if data.phases:
            profile_data["phases"] = [phase.model_dump() for phase in data.phases]
        profile = Profile(**profile_data)
        await profile.insert()
        return profile

    async def get_by_id(self, profile_id: str) -> Profile | None:
        """Get a profile by its ID."""
        return await Profile.get(profile_id)

    async def get_by_id_with_coffee(self, profile_id: str) -> Profile | None:
        """Get a profile by its ID (coffee loaded separately if needed)."""
        return await Profile.get(profile_id)

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Profile]:
        """Get all profiles with pagination."""
        return await Profile.find().sort("-created_at").skip(offset).limit(limit).to_list()

    async def get_by_coffee_id(
        self,
        coffee_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Profile]:
        """Get profiles for a specific coffee."""
        return (
            await Profile.find(Profile.coffee_id == coffee_id)
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_by_roast_level(
        self,
        roast_level: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Profile]:
        """Get profiles filtered by roast level."""
        return (
            await Profile.find(Profile.roast_level == roast_level)
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_by_brew_method(
        self,
        brew_method: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Profile]:
        """Get profiles filtered by brew method."""
        return (
            await Profile.find(Profile.brew_method == brew_method)
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def update(self, profile_id: str, data: ProfileUpdate) -> Profile | None:
        """Update a profile."""
        profile = await self.get_by_id(profile_id)
        if profile is None:
            return None

        update_data = data.model_dump(exclude_unset=True)
        # Convert PhaseSchema objects to dicts if provided
        if "phases" in update_data and data.phases is not None:
            update_data["phases"] = [phase.model_dump() for phase in data.phases]

        update_data["updated_at"] = utc_now()

        for field_name, value in update_data.items():
            setattr(profile, field_name, value)

        # Increment version on update
        profile.version += 1

        await profile.save()
        return profile

    async def delete(self, profile_id: str) -> bool:
        """Delete a profile. SET NULL on roasts that reference it."""
        profile = await self.get_by_id(profile_id)
        if profile is None:
            return False

        # SET NULL: clear profile_id on roasts that used this profile
        await Roast.find(Roast.profile_id == profile_id).update(
            {"$set": {"profile_id": None}}
        )

        await profile.delete()
        return True

    async def count(
        self,
        coffee_id: str | None = None,
        roast_level: str | None = None,
        brew_method: str | None = None,
    ) -> int:
        """Get total count of profiles with optional filters."""
        filters: dict = {}  # type: ignore[type-arg]
        if coffee_id is not None:
            filters["coffee_id"] = coffee_id
        if roast_level is not None:
            filters["roast_level"] = roast_level
        if brew_method is not None:
            filters["brew_method"] = brew_method
        return await Profile.find(filters).count()


# =============================================================================
# Roast Repository
# =============================================================================


class RoastRepository:
    """Repository for Roast entity CRUD operations."""

    async def create(self, data: RoastCreate) -> Roast:
        """Create a new roast session."""
        roast = Roast(**data.model_dump())
        await roast.insert()
        return roast

    async def get_by_id(self, roast_id: str) -> Roast | None:
        """Get a roast by its ID."""
        return await Roast.get(roast_id)

    async def get_by_id_with_relations(self, roast_id: str) -> Roast | None:
        """Get a roast by its ID (relations loaded separately if needed)."""
        return await Roast.get(roast_id)

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Roast]:
        """Get all roasts with pagination."""
        return await Roast.find().sort("-created_at").skip(offset).limit(limit).to_list()

    async def get_all_with_relations(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Roast]:
        """Get all roasts with pagination (relations loaded separately by caller)."""
        return await Roast.find().sort("-created_at").skip(offset).limit(limit).to_list()

    async def get_by_coffee_id(
        self,
        coffee_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Roast]:
        """Get roasts for a specific coffee."""
        return (
            await Roast.find(Roast.coffee_id == coffee_id)
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_by_profile_id(
        self,
        profile_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Roast]:
        """Get roasts using a specific profile."""
        return (
            await Roast.find(Roast.profile_id == profile_id)
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_by_status(
        self,
        status: RoastStatus | str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Roast]:
        """Get roasts filtered by status."""
        return (
            await Roast.find(Roast.status == status)
            .sort("-created_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_in_progress(self) -> Roast | None:
        """Get the most recent in-progress roast, if any."""
        results = (
            await Roast.find(Roast.status == RoastStatus.IN_PROGRESS)
            .sort("-created_at")
            .limit(1)
            .to_list()
        )
        return results[0] if results else None

    async def update(self, roast_id: str, data: RoastUpdate) -> Roast | None:
        """Update a roast session."""
        roast = await self.get_by_id(roast_id)
        if roast is None:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for field_name, value in update_data.items():
            setattr(roast, field_name, value)

        await roast.set(update_data)
        if roast.status in ("completed", "aborted", "abandoned"):
            from maillard.inventory.service import deduct_finished_roast
            await deduct_finished_roast(roast)
        return roast

    async def update_curve_data(self, roast_id: str, curve_data: CurveDataSchema) -> Roast | None:
        """Update the curve data for a roast.

        Args:
            roast_id: The roast's UUID.
            curve_data: CurveDataSchema instance (stored as native subdocument).

        Returns:
            The updated Roast entity or None if not found.
        """
        roast = await self.get_by_id(roast_id)
        if roast is None:
            return None

        await roast.set({"curve_data": curve_data.model_dump()})
        return roast

    async def complete(
        self,
        roast_id: str,
        end_time: str,
        total_time_seconds: int,
        dtr: float | None = None,
        weight_loss: float | None = None,
    ) -> Roast | None:
        """Mark a roast as completed with final metrics."""
        roast = await self.get_by_id(roast_id)
        if roast is None:
            return None

        roast.status = RoastStatus.COMPLETED
        roast.end_time = end_time
        roast.total_time_seconds = total_time_seconds
        if dtr is not None:
            roast.dtr = dtr
        if weight_loss is not None:
            roast.weight_loss = weight_loss

        await roast.set({"status": roast.status, "end_time": roast.end_time,
                         "total_time_seconds": roast.total_time_seconds,
                         "dtr": roast.dtr, "weight_loss": roast.weight_loss})
        from maillard.inventory.service import deduct_finished_roast
        await deduct_finished_roast(roast)
        return roast

    async def abort(self, roast_id: str, end_time: str, notes: str | None = None) -> Roast | None:
        """Mark a roast as aborted."""
        roast = await self.get_by_id(roast_id)
        if roast is None:
            return None

        roast.status = RoastStatus.ABORTED
        roast.end_time = end_time
        if notes is not None:
            roast.notes = notes

        await roast.set({"status": roast.status, "end_time": roast.end_time, "notes": roast.notes})
        from maillard.inventory.service import deduct_finished_roast
        await deduct_finished_roast(roast)
        return roast

    async def delete(self, roast_id: str) -> bool:
        """Delete a roast and cascade to its cuppings."""
        roast = await self.get_by_id(roast_id)
        if roast is None:
            return False

        # CASCADE: delete all cuppings for this roast
        await Cupping.find(Cupping.roast_id == roast_id).delete()

        await roast.delete()
        return True

    async def get_completed_by_coffee_id(
        self,
        coffee_id: str,
        limit: int = 5,
        *,
        exclude_roast_id: str | None = None,
    ) -> Sequence[Roast]:
        """Get completed roasts for a coffee.

        Note: Cuppings are loaded separately by the caller if needed.
        """
        filters: dict = {  # type: ignore[type-arg]
            "coffee_id": coffee_id,
            "status": RoastStatus.COMPLETED,
        }
        if exclude_roast_id is not None:
            filters["_id"] = {"$ne": exclude_roast_id}

        from maillard.ai.history import context_roast_issues

        # Filter before limiting so bad recent rows cannot hide usable history.
        rows = await Roast.find(filters).sort("-start_time").to_list()
        return [r for r in rows if not context_roast_issues(r)][:limit]

    async def count(self, status: RoastStatus | None = None) -> int:
        """Get total count of roasts, optionally filtered by status."""
        filters: dict = {}  # type: ignore[type-arg]
        if status is not None:
            filters["status"] = status
        return await Roast.find(filters).count()


# =============================================================================
# Cupping Repository
# =============================================================================


class CuppingRepository:
    """Repository for Cupping entity CRUD operations."""

    async def create(self, data: CuppingCreate) -> Cupping:
        """Create a new cupping record."""
        cupping_data: dict[str, object] = {
            "roast_id": data.roast_id,
            "rest_days": data.rest_days,
            "brew_method": data.brew_method,
            "cupper_name": data.cupper_name,
            "notes": data.notes,
            "next_time_notes": data.next_time_notes,
        }

        # Extract scores if provided
        if data.scores is not None:
            scores_dict = data.scores.model_dump(exclude_unset=True)
            cupping_data.update(scores_dict)

        # Native lists — no JSON conversion needed
        if data.flavor_notes is not None:
            cupping_data["flavor_notes"] = data.flavor_notes
        if data.defects is not None:
            cupping_data["defects"] = data.defects

        cupping = Cupping(**cupping_data)
        await cupping.insert()
        return cupping

    async def get_by_id(self, cupping_id: str) -> Cupping | None:
        """Get a cupping by its ID."""
        return await Cupping.get(cupping_id)

    async def get_by_id_with_roast(self, cupping_id: str) -> Cupping | None:
        """Get a cupping by its ID (roast loaded separately if needed)."""
        return await Cupping.get(cupping_id)

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cupping]:
        """Get all cuppings with pagination."""
        return await Cupping.find().sort("-cupped_at").skip(offset).limit(limit).to_list()

    async def get_by_roast_id(
        self,
        roast_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cupping]:
        """Get cuppings for a specific roast."""
        return (
            await Cupping.find(Cupping.roast_id == roast_id)
            .sort("-cupped_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_by_coffee_id(
        self,
        coffee_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cupping]:
        """Get cuppings for all roasts of a specific coffee.

        Uses a two-query pattern (replaces SQL JOIN):
        1. Get roast IDs for this coffee
        2. Get cuppings for those roasts
        """
        roasts = await Roast.find(Roast.coffee_id == coffee_id).to_list()
        roast_ids = [r.id for r in roasts]
        if not roast_ids:
            return []
        return (
            await Cupping.find({"roast_id": {"$in": roast_ids}})
            .sort("-cupped_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def get_with_defects(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cupping]:
        """Get cuppings that have recorded defects."""
        return (
            await Cupping.find(
                {
                    "defects": {"$exists": True, "$nin": [None, []]},
                }
            )
            .sort("-cupped_at")
            .skip(offset)
            .limit(limit)
            .to_list()
        )

    async def update(self, cupping_id: str, data: CuppingUpdate) -> Cupping | None:
        """Update a cupping record."""
        cupping = await self.get_by_id(cupping_id)
        if cupping is None:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # Handle scores separately
        if "scores" in update_data and data.scores is not None:
            scores_dict = data.scores.model_dump(exclude_unset=True)
            for field_name, value in scores_dict.items():
                setattr(cupping, field_name, value)
            del update_data["scores"]

        # Native lists — no JSON conversion needed
        for field_name, value in update_data.items():
            setattr(cupping, field_name, value)

        await cupping.save()
        return cupping

    async def delete(self, cupping_id: str) -> bool:
        """Delete a cupping record."""
        cupping = await self.get_by_id(cupping_id)
        if cupping is None:
            return False

        await cupping.delete()
        return True

    async def count(self, roast_id: str | None = None) -> int:
        """Get total count of cuppings, optionally filtered by roast."""
        filters: dict = {}  # type: ignore[type-arg]
        if roast_id is not None:
            filters["roast_id"] = roast_id
        return await Cupping.find(filters).count()


__all__ = [
    "CoffeeRepository",
    "CuppingRepository",
    "ProfileRepository",
    "RoastRepository",
    "RoastStatus",
]
