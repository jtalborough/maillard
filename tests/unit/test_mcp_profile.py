"""Unit tests for MCP profile management tools."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from maillard.db.repository import ProfileRepository
from maillard.mcp import lifespan
from maillard.mcp.tools.coffee import add_coffee
from maillard.mcp.tools.profile import (
    delete_profile,
    generate_profile,
    get_profile,
    list_profiles,
    save_profile,
    update_profile,
)


# Note: reset_global_state fixture from conftest.py is applied automatically


def _make_mock_context(state: Any) -> Any:
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


def _make_valid_phases() -> list[dict[str, Any]]:
    """Create a valid phases list for testing."""
    return [
        {
            "name": "charge",
            "target_time_seconds": 0,
            "target_temp": 195.0,
            "heat_percent": 100,
            "fan_percent": 40,
            "drum_percent": 60,
        },
        {
            "name": "turning_point",
            "target_time_seconds": 75,
            "target_temp": 97.0,
            "heat_percent": 80,
            "fan_percent": 40,
            "drum_percent": 60,
            "target_ror": 12.0,
        },
        {
            "name": "dry_end",
            "target_time_seconds": 240,
            "target_temp": 150.0,
            "heat_percent": 70,
            "fan_percent": 50,
            "drum_percent": 60,
        },
        {
            "name": "fc_start",
            "target_time_seconds": 480,
            "target_temp": 196.0,
            "heat_percent": 50,
            "fan_percent": 60,
            "drum_percent": 60,
            "target_ror": 8.0,
        },
        {
            "name": "drop",
            "target_time_seconds": 600,
            "target_temp": 210.0,
            "heat_percent": 0,
            "fan_percent": 100,
            "drum_percent": 60,
            "notes": "Target 15% development",
        },
    ]


# =============================================================================
# save_profile Tests
# =============================================================================


class TestSaveProfile:
    """Tests for save_profile tool."""

    @pytest.mark.asyncio
    async def test_save_profile_minimal(self, mongo_db) -> None:
        """Should save a profile with only required fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = [
                {
                    "name": "charge",
                    "target_time_seconds": 0,
                    "target_temp": 195.0,
                    "heat_percent": 100,
                    "fan_percent": 40,
                    "drum_percent": 60,
                }
            ]

            result = await save_profile(ctx, name="Test Profile", phases=phases)

            assert result["id"] is not None
            assert result["name"] == "Test Profile"
            assert len(result["phases"]) == 1
            assert result["phases"][0]["name"] == "charge"
            assert result["version"] == 1
            assert result["created_at"] is not None

    @pytest.mark.asyncio
    async def test_save_profile_full(self, mongo_db) -> None:
        """Should save a profile with all fields populated."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # First create a coffee to associate
            coffee = await add_coffee(ctx, name="Test Coffee", origin="Ethiopia")
            phases = _make_valid_phases()

            result = await save_profile(
                ctx,
                name="Full Profile",
                phases=phases,
                coffee_id=coffee["id"],
                roast_level="Medium",
                flavor_goal="Highlight floral and citrus notes",
                brew_method="Filter",
                batch_size_grams=300,
                target_dtr=18.0,
                target_weight_loss=14.0,
                charge_temp=195.0,
                ai_rationale="AI-generated based on origin characteristics",
            )

            assert result["id"] is not None
            assert result["name"] == "Full Profile"
            assert result["coffee_id"] == coffee["id"]
            assert result["roast_level"] == "Medium"
            assert result["flavor_goal"] == "Highlight floral and citrus notes"
            assert result["brew_method"] == "Filter"
            assert result["batch_size_grams"] == 300
            assert result["target_dtr"] == 18.0
            assert result["target_weight_loss"] == 14.0
            assert result["charge_temp"] == 195.0
            assert result["ai_rationale"] == "AI-generated based on origin characteristics"
            assert len(result["phases"]) == 5

    @pytest.mark.asyncio
    async def test_save_profile_empty_phases_fails(self, mongo_db) -> None:
        """Should fail when phases list is empty."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="At least one phase is required"):
                await save_profile(ctx, name="Empty Phases", phases=[])

    @pytest.mark.asyncio
    async def test_save_profile_invalid_phase_fails(self, mongo_db) -> None:
        """Should fail when phase data is invalid."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Missing required fields
            invalid_phases = [{"name": "charge"}]  # Missing other required fields

            with pytest.raises(ValueError, match="Invalid phase data"):
                await save_profile(ctx, name="Invalid Phases", phases=invalid_phases)

    @pytest.mark.asyncio
    async def test_save_profile_persists(self, mongo_db) -> None:
        """Should persist the profile to the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            result = await save_profile(ctx, name="Persisted Profile", phases=phases)
            profile_id = result["id"]

            # Verify it's in the database
            repo = ProfileRepository()
            profile = await repo.get_by_id(profile_id)

            assert profile is not None
            assert profile.name == "Persisted Profile"


# =============================================================================
# list_profiles Tests
# =============================================================================


class TestListProfiles:
    """Tests for list_profiles tool."""

    @pytest.mark.asyncio
    async def test_list_profiles_empty(self, mongo_db) -> None:
        """Should return empty list when no profiles exist."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await list_profiles(ctx)

            assert result["profiles"] == []
            assert result["total"] == 0
            assert result["limit"] == 50
            assert result["offset"] == 0

    @pytest.mark.asyncio
    async def test_list_profiles_returns_summaries(self, mongo_db) -> None:
        """Should return profile summaries with basic info."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()

            # Add some profiles
            await save_profile(
                ctx,
                name="Profile A",
                phases=phases,
                roast_level="Light",
                brew_method="Filter",
            )
            await save_profile(
                ctx,
                name="Profile B",
                phases=phases,
                roast_level="Medium",
                brew_method="Espresso",
            )

            result = await list_profiles(ctx)

            assert result["total"] == 2
            assert len(result["profiles"]) == 2

            # Check summary format
            for summary in result["profiles"]:
                assert "id" in summary
                assert "name" in summary
                assert "coffee_id" in summary
                assert "roast_level" in summary
                assert "brew_method" in summary

    @pytest.mark.asyncio
    async def test_list_profiles_filter_by_coffee_id(self, mongo_db) -> None:
        """Should filter profiles by coffee ID."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()

            # Create coffees
            coffee1 = await add_coffee(ctx, name="Coffee 1", origin="Ethiopia")
            coffee2 = await add_coffee(ctx, name="Coffee 2", origin="Colombia")

            # Create profiles for different coffees
            await save_profile(ctx, name="Ethiopia 1", phases=phases, coffee_id=coffee1["id"])
            await save_profile(ctx, name="Ethiopia 2", phases=phases, coffee_id=coffee1["id"])
            await save_profile(ctx, name="Colombia 1", phases=phases, coffee_id=coffee2["id"])

            result = await list_profiles(ctx, coffee_id=coffee1["id"])

            assert result["total"] == 2
            for profile in result["profiles"]:
                assert profile["coffee_id"] == coffee1["id"]

    @pytest.mark.asyncio
    async def test_list_profiles_filter_by_roast_level(self, mongo_db) -> None:
        """Should filter profiles by roast level."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()

            await save_profile(ctx, name="Light 1", phases=phases, roast_level="Light")
            await save_profile(ctx, name="Light 2", phases=phases, roast_level="Light")
            await save_profile(ctx, name="Medium 1", phases=phases, roast_level="Medium")

            result = await list_profiles(ctx, roast_level="Light")

            assert len(result["profiles"]) == 2
            for profile in result["profiles"]:
                assert profile["roast_level"] == "Light"

    @pytest.mark.asyncio
    async def test_list_profiles_filter_by_brew_method(self, mongo_db) -> None:
        """Should filter profiles by brew method."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()

            await save_profile(ctx, name="Filter 1", phases=phases, brew_method="Filter")
            await save_profile(ctx, name="Filter 2", phases=phases, brew_method="Filter")
            await save_profile(ctx, name="Espresso 1", phases=phases, brew_method="Espresso")

            result = await list_profiles(ctx, brew_method="Espresso")

            assert len(result["profiles"]) == 1
            assert result["profiles"][0]["brew_method"] == "Espresso"

    @pytest.mark.asyncio
    async def test_list_profiles_pagination(self, mongo_db) -> None:
        """Should support pagination with limit and offset."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()

            # Add 5 profiles
            for i in range(5):
                await save_profile(ctx, name=f"Profile {i}", phases=phases)

            # Get first page
            page1 = await list_profiles(ctx, limit=2, offset=0)
            assert len(page1["profiles"]) == 2
            assert page1["total"] == 5

            # Get second page
            page2 = await list_profiles(ctx, limit=2, offset=2)
            assert len(page2["profiles"]) == 2
            assert page2["offset"] == 2

            # Get last page (partial)
            page3 = await list_profiles(ctx, limit=2, offset=4)
            assert len(page3["profiles"]) == 1

    @pytest.mark.asyncio
    async def test_list_profiles_limit_clamped(self, mongo_db) -> None:
        """Should clamp limit to max 100."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await list_profiles(ctx, limit=500)

            assert result["limit"] == 100

    @pytest.mark.asyncio
    async def test_list_profiles_multiple_filters_fails(self, mongo_db) -> None:
        """Should reject multiple filters being applied at once."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Try to use two filters at once
            with pytest.raises(ValueError, match="Only one filter can be applied"):
                await list_profiles(ctx, coffee_id="some-id", roast_level="Light")

            # Try all three filters
            with pytest.raises(ValueError, match="Only one filter can be applied"):
                await list_profiles(
                    ctx, coffee_id="some-id", roast_level="Light", brew_method="Filter"
                )


# =============================================================================
# get_profile Tests
# =============================================================================


class TestGetProfile:
    """Tests for get_profile tool."""

    @pytest.mark.asyncio
    async def test_get_profile_returns_full_details(self, mongo_db) -> None:
        """Should return full profile details including all fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(
                ctx,
                name="Test Profile",
                phases=phases,
                roast_level="Medium",
                flavor_goal="Balanced sweetness",
                brew_method="Both",
                target_dtr=17.5,
            )

            result = await get_profile(ctx, saved["id"])

            assert result["id"] == saved["id"]
            assert result["name"] == "Test Profile"
            assert result["roast_level"] == "Medium"
            assert result["flavor_goal"] == "Balanced sweetness"
            assert result["brew_method"] == "Both"
            assert result["target_dtr"] == 17.5
            assert len(result["phases"]) == 5
            assert result["version"] == 1
            assert result["created_at"] is not None
            assert result["updated_at"] is not None

    @pytest.mark.asyncio
    async def test_get_profile_phases_deserialized(self, mongo_db) -> None:
        """Should return phases as deserialized list of dicts."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Test Profile", phases=phases)

            result = await get_profile(ctx, saved["id"])

            # Verify phases structure
            assert isinstance(result["phases"], list)
            charge_phase = result["phases"][0]
            assert charge_phase["name"] == "charge"
            assert charge_phase["target_time_seconds"] == 0
            assert charge_phase["target_temp"] == 195.0
            assert charge_phase["heat_percent"] == 100
            assert charge_phase["fan_percent"] == 40
            assert charge_phase["drum_percent"] == 60

    @pytest.mark.asyncio
    async def test_get_profile_not_found(self, mongo_db) -> None:
        """Should raise ValueError when profile not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Profile not found"):
                await get_profile(ctx, "non-existent-uuid")


# =============================================================================
# update_profile Tests
# =============================================================================


class TestUpdateProfile:
    """Tests for update_profile tool."""

    @pytest.mark.asyncio
    async def test_update_profile_single_field(self, mongo_db) -> None:
        """Should update a single field and increment version."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Original Name", phases=phases)
            assert saved["version"] == 1

            result = await update_profile(ctx, saved["id"], name="Updated Name")

            assert result["name"] == "Updated Name"
            assert result["version"] == 2  # Version incremented

    @pytest.mark.asyncio
    async def test_update_profile_multiple_fields(self, mongo_db) -> None:
        """Should update multiple fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Test", phases=phases)

            result = await update_profile(
                ctx,
                saved["id"],
                name="Updated Name",
                roast_level="Dark",
                flavor_goal="Bold and chocolatey",
                target_dtr=22.0,
            )

            assert result["name"] == "Updated Name"
            assert result["roast_level"] == "Dark"
            assert result["flavor_goal"] == "Bold and chocolatey"
            assert result["target_dtr"] == 22.0

    @pytest.mark.asyncio
    async def test_update_profile_phases(self, mongo_db) -> None:
        """Should update phases completely."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            original_phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Test", phases=original_phases)

            # Update with new phases
            new_phases = [
                {
                    "name": "charge",
                    "target_time_seconds": 0,
                    "target_temp": 200.0,  # Different temp
                    "heat_percent": 90,
                    "fan_percent": 45,
                    "drum_percent": 65,
                },
                {
                    "name": "drop",
                    "target_time_seconds": 720,  # Longer roast
                    "target_temp": 220.0,
                    "heat_percent": 0,
                    "fan_percent": 100,
                    "drum_percent": 60,
                },
            ]

            result = await update_profile(ctx, saved["id"], phases=new_phases)

            assert len(result["phases"]) == 2
            assert result["phases"][0]["target_temp"] == 200.0
            assert result["phases"][1]["target_time_seconds"] == 720

    @pytest.mark.asyncio
    async def test_update_profile_invalid_phases_fails(self, mongo_db) -> None:
        """Should fail when updated phase data is invalid."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Test", phases=phases)

            invalid_phases = [{"name": "invalid"}]

            with pytest.raises(ValueError, match="Invalid phase data"):
                await update_profile(ctx, saved["id"], phases=invalid_phases)

    @pytest.mark.asyncio
    async def test_update_profile_not_found(self, mongo_db) -> None:
        """Should raise ValueError when profile not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Profile not found"):
                await update_profile(ctx, "non-existent-uuid", name="New Name")

    @pytest.mark.asyncio
    async def test_update_profile_no_fields_provided(self, mongo_db) -> None:
        """Should raise ValueError when no fields to update."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Test", phases=phases)

            with pytest.raises(ValueError, match="No fields provided"):
                await update_profile(ctx, saved["id"])

    @pytest.mark.asyncio
    async def test_update_profile_persists(self, mongo_db) -> None:
        """Should persist updates to the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="Original", phases=phases)
            await update_profile(ctx, saved["id"], name="Updated")

            # Fetch fresh from database
            fetched = await get_profile(ctx, saved["id"])
            assert fetched["name"] == "Updated"
            assert fetched["version"] == 2


# =============================================================================
# delete_profile Tests
# =============================================================================


class TestDeleteProfile:
    """Tests for delete_profile tool."""

    @pytest.mark.asyncio
    async def test_delete_profile_success(self, mongo_db) -> None:
        """Should delete a profile and return confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="To Delete", phases=phases)

            result = await delete_profile(ctx, saved["id"])

            assert result["deleted"] is True
            assert result["profile_id"] == saved["id"]

    @pytest.mark.asyncio
    async def test_delete_profile_removes_from_database(self, mongo_db) -> None:
        """Should remove the profile from the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()
            saved = await save_profile(ctx, name="To Delete", phases=phases)
            await delete_profile(ctx, saved["id"])

            # Verify it's gone
            with pytest.raises(ValueError, match="Profile not found"):
                await get_profile(ctx, saved["id"])

    @pytest.mark.asyncio
    async def test_delete_profile_not_found(self, mongo_db) -> None:
        """Should raise ValueError when profile not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Profile not found"):
                await delete_profile(ctx, "non-existent-uuid")


# =============================================================================
# generate_profile Tests
# =============================================================================


class TestGenerateProfile:
    """Tests for generate_profile tool."""

    @pytest.mark.asyncio
    async def test_generate_profile_returns_heuristic(self, mongo_db) -> None:
        """Should return a heuristic profile with 7 phases."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            coffee = await add_coffee(ctx, name="Test Coffee", origin="Ethiopia")

            result = await generate_profile(
                ctx,
                coffee_id=coffee["id"],
                roast_level="Medium",
                flavor_goal="Highlight floral notes",
                brew_method="Filter",
                batch_size_grams=300,
            )

            assert result["source"] == "heuristic"
            assert len(result["phases"]) == 7
            assert result["charge_temp"] is not None
            assert result["target_dtr"] is not None
            assert result["ai_rationale"] is not None
            assert "Test Coffee" in result["name"]

    @pytest.mark.asyncio
    async def test_generate_profile_coffee_not_found(self, mongo_db) -> None:
        """Should raise ValueError when coffee not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Coffee not found"):
                await generate_profile(
                    ctx,
                    coffee_id="non-existent-uuid",
                    roast_level="Medium",
                    flavor_goal="Balanced",
                    brew_method="Filter",
                    batch_size_grams=300,
                )


# =============================================================================
# Integration Tests
# =============================================================================


class TestProfileToolsIntegration:
    """Integration tests for profile tools working together."""

    @pytest.mark.asyncio
    async def test_full_crud_workflow(self, mongo_db) -> None:
        """Test a complete CRUD workflow."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            phases = _make_valid_phases()

            # Create
            created = await save_profile(
                ctx,
                name="Integration Test Profile",
                phases=phases,
                roast_level="Medium-Light",
                brew_method="Filter",
            )
            profile_id = created["id"]
            assert profile_id is not None

            # Read
            retrieved = await get_profile(ctx, profile_id)
            assert retrieved["name"] == "Integration Test Profile"
            assert len(retrieved["phases"]) == 5

            # List (should include our profile)
            listed = await list_profiles(ctx)
            assert any(p["id"] == profile_id for p in listed["profiles"])

            # Update
            updated = await update_profile(
                ctx,
                profile_id,
                name="Updated Integration Test Profile",
                flavor_goal="Enhanced sweetness",
            )
            assert updated["name"] == "Updated Integration Test Profile"
            assert updated["flavor_goal"] == "Enhanced sweetness"
            assert updated["roast_level"] == "Medium-Light"  # Unchanged
            assert updated["version"] == 2

            # Verify update persisted
            retrieved_again = await get_profile(ctx, profile_id)
            assert retrieved_again["name"] == "Updated Integration Test Profile"

            # Delete
            delete_result = await delete_profile(ctx, profile_id)
            assert delete_result["deleted"] is True

            # Verify deleted
            with pytest.raises(ValueError, match="Profile not found"):
                await get_profile(ctx, profile_id)

            # List should no longer include it
            final_list = await list_profiles(ctx)
            assert not any(p["id"] == profile_id for p in final_list["profiles"])

    @pytest.mark.asyncio
    async def test_profile_with_coffee_relationship(self, mongo_db) -> None:
        """Test creating profiles associated with coffees."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Create coffees
            ethiopia = await add_coffee(
                ctx,
                name="Ethiopia Yirgacheffe",
                origin="Ethiopia",
                processing="Natural",
            )
            colombia = await add_coffee(
                ctx,
                name="Colombia Huila",
                origin="Colombia",
                processing="Washed",
            )

            phases = _make_valid_phases()

            # Create profiles for each coffee
            ethiopia_profile = await save_profile(
                ctx,
                name="Ethiopia Light",
                phases=phases,
                coffee_id=ethiopia["id"],
                roast_level="Light",
            )
            colombia_profile = await save_profile(
                ctx,
                name="Colombia Medium",
                phases=phases,
                coffee_id=colombia["id"],
                roast_level="Medium",
            )

            # Filter by coffee should work
            ethiopia_profiles = await list_profiles(ctx, coffee_id=ethiopia["id"])
            assert len(ethiopia_profiles["profiles"]) == 1
            assert ethiopia_profiles["profiles"][0]["id"] == ethiopia_profile["id"]

            colombia_profiles = await list_profiles(ctx, coffee_id=colombia["id"])
            assert len(colombia_profiles["profiles"]) == 1
            assert colombia_profiles["profiles"][0]["id"] == colombia_profile["id"]
