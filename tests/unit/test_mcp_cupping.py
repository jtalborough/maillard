"""Unit tests for MCP cupping management tools."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from maillard.db.repository import CoffeeRepository, CuppingRepository, RoastRepository
from maillard.db.schemas import CoffeeCreate, RoastCreate
from maillard.mcp import lifespan
from maillard.mcp.tools.cupping import (
    add_cupping,
    delete_cupping,
    get_cupping,
    list_cuppings,
    update_cupping,
)


# Note: reset_global_state fixture from conftest.py is applied automatically


def _make_mock_context(state: Any) -> Any:
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


async def _create_test_coffee(state: Any) -> str:
    """Create a test coffee and return its ID."""
    repo = CoffeeRepository()
    coffee = await repo.create(
        CoffeeCreate(name="Test Coffee", origin="Ethiopia")
    )
    return coffee.id


async def _create_test_roast(state: Any, coffee_id: str | None = None) -> str:
    """Create a test roast and return its ID."""
    repo = RoastRepository()
    roast = await repo.create(RoastCreate(coffee_id=coffee_id))
    return roast.id


# =============================================================================
# add_cupping Tests
# =============================================================================


class TestAddCupping:
    """Tests for add_cupping tool."""

    @pytest.mark.asyncio
    async def test_add_cupping_minimal(self, mongo_db) -> None:
        """Should add a cupping with only roast_id."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(ctx, roast_id=roast_id)

            assert result["id"] is not None
            assert result["roast_id"] == roast_id
            assert result["total_score"] is None
            assert result["cupped_at"] is not None

    @pytest.mark.asyncio
    async def test_add_cupping_with_scores(self, mongo_db) -> None:
        """Should add a cupping with all SCA scores."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.5,
                acidity=8.0,
                sweetness=7.5,
                body=7.0,
                aftertaste=8.0,
                overall=8.0,
            )

            assert result["aroma"] == 8.5
            assert result["acidity"] == 8.0
            assert result["sweetness"] == 7.5
            assert result["body"] == 7.0
            assert result["aftertaste"] == 8.0
            assert result["overall"] == 8.0
            # Total: 8.5 + 8.0 + 7.5 + 7.0 + 8.0 + 8.0 = 47.0
            assert result["total_score"] == 47.0

    @pytest.mark.asyncio
    async def test_add_cupping_with_partial_scores(self, mongo_db) -> None:
        """Should calculate total from partial scores."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.0,
                overall=7.5,
            )

            assert result["aroma"] == 8.0
            assert result["overall"] == 7.5
            assert result["acidity"] is None
            # Total: 8.0 + 7.5 = 15.5
            assert result["total_score"] == 15.5

    @pytest.mark.asyncio
    async def test_add_cupping_full(self, mongo_db) -> None:
        """Should add a cupping with all fields populated."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.5,
                acidity=8.0,
                sweetness=7.5,
                body=7.0,
                aftertaste=8.0,
                overall=8.0,
                flavor_notes=["citrus", "floral", "honey"],
                defects=["slight bitterness"],
                rest_days=7,
                brew_method="V60",
                notes="Excellent clarity with bright citrus notes",
            )

            assert result["id"] is not None
            assert result["roast_id"] == roast_id
            assert result["flavor_notes"] == ["citrus", "floral", "honey"]
            assert result["defects"] == ["slight bitterness"]
            assert result["rest_days"] == 7
            assert result["brew_method"] == "V60"
            assert result["notes"] == "Excellent clarity with bright citrus notes"
            assert result["total_score"] == 47.0

    @pytest.mark.asyncio
    async def test_add_cupping_invalid_roast(self, mongo_db) -> None:
        """Should raise ValueError when roast_id doesn't exist."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Roast not found"):
                await add_cupping(ctx, roast_id="non-existent-uuid")

    @pytest.mark.asyncio
    async def test_add_cupping_persists(self, mongo_db) -> None:
        """Should persist the cupping to the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.0,
                notes="Test persistence",
            )
            cupping_id = result["id"]

            # Verify it's in the database
            repo = CuppingRepository()
            cupping = await repo.get_by_id(cupping_id)

            assert cupping is not None
            assert cupping.aroma == 8.0
            assert cupping.notes == "Test persistence"


# =============================================================================
# list_cuppings Tests
# =============================================================================


class TestListCuppings:
    """Tests for list_cuppings tool."""

    @pytest.mark.asyncio
    async def test_list_cuppings_empty(self, mongo_db) -> None:
        """Should return empty list when no cuppings exist."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await list_cuppings(ctx)

            assert result["cuppings"] == []
            assert result["total"] == 0
            assert result["limit"] == 50
            assert result["offset"] == 0

    @pytest.mark.asyncio
    async def test_list_cuppings_returns_summaries(self, mongo_db) -> None:
        """Should return cupping summaries with basic info."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            # Add some cuppings
            await add_cupping(ctx, roast_id=roast_id, aroma=8.0, overall=7.5)
            await add_cupping(ctx, roast_id=roast_id, aroma=7.0, overall=8.0)

            result = await list_cuppings(ctx)

            assert result["total"] == 2
            assert len(result["cuppings"]) == 2

            # Check summary format
            for summary in result["cuppings"]:
                assert "id" in summary
                assert "roast_id" in summary
                assert "total_score" in summary
                assert "cupped_at" in summary

    @pytest.mark.asyncio
    async def test_list_cuppings_filter_by_roast_id(self, mongo_db) -> None:
        """Should filter cuppings by roast_id."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id_1 = await _create_test_roast(state)
            roast_id_2 = await _create_test_roast(state)

            await add_cupping(ctx, roast_id=roast_id_1, aroma=8.0)
            await add_cupping(ctx, roast_id=roast_id_1, aroma=7.5)
            await add_cupping(ctx, roast_id=roast_id_2, aroma=9.0)

            result = await list_cuppings(ctx, roast_id=roast_id_1)

            assert result["total"] == 2
            assert len(result["cuppings"]) == 2
            for cupping in result["cuppings"]:
                assert cupping["roast_id"] == roast_id_1

    @pytest.mark.asyncio
    async def test_list_cuppings_filter_by_coffee_id(self, mongo_db) -> None:
        """Should filter cuppings by coffee_id through roast join."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Create two coffees with roasts
            coffee_id_1 = await _create_test_coffee(state)
            coffee_id_2 = await _create_test_coffee(state)
            roast_1a = await _create_test_roast(state, coffee_id=coffee_id_1)
            roast_1b = await _create_test_roast(state, coffee_id=coffee_id_1)
            roast_2 = await _create_test_roast(state, coffee_id=coffee_id_2)

            # Add cuppings to each roast
            await add_cupping(ctx, roast_id=roast_1a, aroma=8.0)
            await add_cupping(ctx, roast_id=roast_1b, aroma=7.5)
            await add_cupping(ctx, roast_id=roast_2, aroma=9.0)

            result = await list_cuppings(ctx, coffee_id=coffee_id_1)

            assert result["total"] == 2
            assert len(result["cuppings"]) == 2

    @pytest.mark.asyncio
    async def test_list_cuppings_pagination(self, mongo_db) -> None:
        """Should support pagination with limit and offset."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            # Add 5 cuppings
            for i in range(5):
                await add_cupping(ctx, roast_id=roast_id, aroma=float(i + 5))

            # Get first page
            page1 = await list_cuppings(ctx, limit=2, offset=0)
            assert len(page1["cuppings"]) == 2
            assert page1["total"] == 5

            # Get second page
            page2 = await list_cuppings(ctx, limit=2, offset=2)
            assert len(page2["cuppings"]) == 2
            assert page2["offset"] == 2

            # Get last page (partial)
            page3 = await list_cuppings(ctx, limit=2, offset=4)
            assert len(page3["cuppings"]) == 1

    @pytest.mark.asyncio
    async def test_list_cuppings_limit_clamped(self, mongo_db) -> None:
        """Should clamp limit to max 100."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await list_cuppings(ctx, limit=500)

            assert result["limit"] == 100


# =============================================================================
# get_cupping Tests
# =============================================================================


class TestGetCupping:
    """Tests for get_cupping tool."""

    @pytest.mark.asyncio
    async def test_get_cupping_returns_full_details(self, mongo_db) -> None:
        """Should return full cupping details including all fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.5,
                acidity=8.0,
                sweetness=7.5,
                body=7.0,
                aftertaste=8.0,
                overall=8.0,
                flavor_notes=["citrus", "honey"],
                defects=["slight bitterness"],
                rest_days=5,
                brew_method="Cupping bowl",
                notes="Test cupping notes",
            )

            result = await get_cupping(ctx, added["id"])

            assert result["id"] == added["id"]
            assert result["roast_id"] == roast_id
            assert result["aroma"] == 8.5
            assert result["acidity"] == 8.0
            assert result["sweetness"] == 7.5
            assert result["body"] == 7.0
            assert result["aftertaste"] == 8.0
            assert result["overall"] == 8.0
            assert result["total_score"] == 47.0
            assert result["flavor_notes"] == ["citrus", "honey"]
            assert result["defects"] == ["slight bitterness"]
            assert result["rest_days"] == 5
            assert result["brew_method"] == "Cupping bowl"
            assert result["notes"] == "Test cupping notes"
            assert result["cupped_at"] is not None

    @pytest.mark.asyncio
    async def test_get_cupping_not_found(self, mongo_db) -> None:
        """Should raise ValueError when cupping not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Cupping not found"):
                await get_cupping(ctx, "non-existent-uuid")


# =============================================================================
# update_cupping Tests
# =============================================================================


class TestUpdateCupping:
    """Tests for update_cupping tool."""

    @pytest.mark.asyncio
    async def test_update_cupping_single_score(self, mongo_db) -> None:
        """Should update a single score field."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(ctx, roast_id=roast_id, aroma=7.0)

            result = await update_cupping(ctx, added["id"], aroma=8.5)

            assert result["aroma"] == 8.5
            assert result["total_score"] == 8.5

    @pytest.mark.asyncio
    async def test_update_cupping_multiple_fields(self, mongo_db) -> None:
        """Should update multiple fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(ctx, roast_id=roast_id, aroma=7.0)

            result = await update_cupping(
                ctx,
                added["id"],
                aroma=8.5,
                acidity=8.0,
                notes="Updated notes",
                rest_days=10,
            )

            assert result["aroma"] == 8.5
            assert result["acidity"] == 8.0
            assert result["notes"] == "Updated notes"
            assert result["rest_days"] == 10
            # Total: 8.5 + 8.0 = 16.5
            assert result["total_score"] == 16.5

    @pytest.mark.asyncio
    async def test_update_cupping_flavor_notes(self, mongo_db) -> None:
        """Should update flavor notes list."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(
                ctx, roast_id=roast_id, flavor_notes=["original"]
            )

            result = await update_cupping(
                ctx, added["id"], flavor_notes=["updated", "notes"]
            )

            assert result["flavor_notes"] == ["updated", "notes"]

    @pytest.mark.asyncio
    async def test_update_cupping_not_found(self, mongo_db) -> None:
        """Should raise ValueError when cupping not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Cupping not found"):
                await update_cupping(ctx, "non-existent-uuid", aroma=8.0)

    @pytest.mark.asyncio
    async def test_update_cupping_no_fields_provided(self, mongo_db) -> None:
        """Should raise ValueError when no fields to update."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(ctx, roast_id=roast_id, aroma=7.0)

            with pytest.raises(ValueError, match="No fields provided"):
                await update_cupping(ctx, added["id"])

    @pytest.mark.asyncio
    async def test_update_cupping_persists(self, mongo_db) -> None:
        """Should persist updates to the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(ctx, roast_id=roast_id, aroma=7.0)
            await update_cupping(ctx, added["id"], aroma=9.0)

            # Fetch fresh from database
            fetched = await get_cupping(ctx, added["id"])
            assert fetched["aroma"] == 9.0


# =============================================================================
# delete_cupping Tests
# =============================================================================


class TestDeleteCupping:
    """Tests for delete_cupping tool."""

    @pytest.mark.asyncio
    async def test_delete_cupping_success(self, mongo_db) -> None:
        """Should delete a cupping and return confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(ctx, roast_id=roast_id, aroma=8.0)

            result = await delete_cupping(ctx, added["id"])

            assert result["deleted"] is True
            assert result["cupping_id"] == added["id"]

    @pytest.mark.asyncio
    async def test_delete_cupping_removes_from_database(self, mongo_db) -> None:
        """Should remove the cupping from the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            added = await add_cupping(ctx, roast_id=roast_id, aroma=8.0)
            await delete_cupping(ctx, added["id"])

            # Verify it's gone
            with pytest.raises(ValueError, match="Cupping not found"):
                await get_cupping(ctx, added["id"])

    @pytest.mark.asyncio
    async def test_delete_cupping_not_found(self, mongo_db) -> None:
        """Should raise ValueError when cupping not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Cupping not found"):
                await delete_cupping(ctx, "non-existent-uuid")


# =============================================================================
# Integration Tests
# =============================================================================


class TestCuppingToolsIntegration:
    """Integration tests for cupping tools working together."""

    @pytest.mark.asyncio
    async def test_full_crud_workflow(self, mongo_db) -> None:
        """Test a complete CRUD workflow."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            # Create
            created = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.0,
                acidity=7.5,
                sweetness=8.0,
                body=7.0,
                aftertaste=7.5,
                overall=8.0,
                flavor_notes=["citrus", "chocolate"],
                notes="Initial cupping",
            )
            cupping_id = created["id"]
            assert cupping_id is not None
            assert created["total_score"] == 46.0

            # Read
            retrieved = await get_cupping(ctx, cupping_id)
            assert retrieved["aroma"] == 8.0
            assert retrieved["flavor_notes"] == ["citrus", "chocolate"]

            # List (should include our cupping)
            listed = await list_cuppings(ctx)
            assert any(c["id"] == cupping_id for c in listed["cuppings"])

            # Update
            updated = await update_cupping(
                ctx,
                cupping_id,
                overall=8.5,
                notes="Revised after second tasting",
            )
            assert updated["overall"] == 8.5
            assert updated["notes"] == "Revised after second tasting"
            assert updated["aroma"] == 8.0  # Unchanged
            # New total: 8.0 + 7.5 + 8.0 + 7.0 + 7.5 + 8.5 = 46.5
            assert updated["total_score"] == 46.5

            # Verify update persisted
            retrieved_again = await get_cupping(ctx, cupping_id)
            assert retrieved_again["overall"] == 8.5

            # Delete
            delete_result = await delete_cupping(ctx, cupping_id)
            assert delete_result["deleted"] is True

            # Verify deleted
            with pytest.raises(ValueError, match="Cupping not found"):
                await get_cupping(ctx, cupping_id)

            # List should no longer include it
            final_list = await list_cuppings(ctx)
            assert not any(c["id"] == cupping_id for c in final_list["cuppings"])

    @pytest.mark.asyncio
    async def test_multiple_cuppings_per_roast(self, mongo_db) -> None:
        """Test adding multiple cuppings to the same roast."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            # Add multiple cuppings at different rest days
            cupping_day_3 = await add_cupping(
                ctx, roast_id=roast_id, rest_days=3, aroma=7.0, overall=7.0
            )
            cupping_day_7 = await add_cupping(
                ctx, roast_id=roast_id, rest_days=7, aroma=8.0, overall=8.0
            )
            cupping_day_14 = await add_cupping(
                ctx, roast_id=roast_id, rest_days=14, aroma=8.5, overall=8.5
            )

            # List all cuppings for this roast
            result = await list_cuppings(ctx, roast_id=roast_id)
            assert result["total"] == 3

            # Verify each cupping exists
            cupping_ids = {c["id"] for c in result["cuppings"]}
            assert cupping_day_3["id"] in cupping_ids
            assert cupping_day_7["id"] in cupping_ids
            assert cupping_day_14["id"] in cupping_ids


class TestTotalScoreCalculation:
    """Tests for total score calculation."""

    @pytest.mark.asyncio
    async def test_total_score_all_scores(self, mongo_db) -> None:
        """Should sum all 6 scores correctly."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=10.0,
                acidity=10.0,
                sweetness=10.0,
                body=10.0,
                aftertaste=10.0,
                overall=10.0,
            )

            # Max possible: 6 × 10 = 60
            assert result["total_score"] == 60.0

    @pytest.mark.asyncio
    async def test_total_score_no_scores(self, mongo_db) -> None:
        """Should return None when no scores provided."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                notes="Just notes, no scores",
            )

            assert result["total_score"] is None

    @pytest.mark.asyncio
    async def test_total_score_partial(self, mongo_db) -> None:
        """Should sum only provided scores."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            roast_id = await _create_test_roast(state)

            result = await add_cupping(
                ctx,
                roast_id=roast_id,
                aroma=8.0,
                body=7.0,
                overall=8.5,
            )

            # Only 3 scores: 8.0 + 7.0 + 8.5 = 23.5
            assert result["total_score"] == 23.5
