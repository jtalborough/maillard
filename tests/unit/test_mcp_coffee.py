"""Unit tests for MCP coffee management tools."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from maillard.db.models import Coffee
from maillard.db.repository import CoffeeRepository
from maillard.mcp import lifespan
from maillard.mcp.tools.coffee import (
    add_coffee,
    delete_coffee,
    get_coffee,
    list_coffees,
    update_coffee,
)

pytestmark = pytest.mark.usefixtures("mongo_db")

# Note: reset_global_state fixture from conftest.py is applied automatically


def _make_mock_context(state: Any) -> Any:
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


# =============================================================================
# add_coffee Tests
# =============================================================================


class TestAddCoffee:
    """Tests for add_coffee tool."""

    @pytest.mark.asyncio
    async def test_add_coffee_minimal(self) -> None:
        """Should add a coffee with only required fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await add_coffee(ctx, name="Test Coffee", origin="Ethiopia")

            assert result["id"] is not None
            assert result["name"] == "Test Coffee"
            assert result["origin"] == "Ethiopia"
            assert result["processing"] is None
            assert result["created_at"] is not None
            assert result["updated_at"] is not None

    @pytest.mark.asyncio
    async def test_add_coffee_full(self) -> None:
        """Should add a coffee with all fields populated."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await add_coffee(
                ctx,
                name="Yirgacheffe Natural",
                origin="Ethiopia",
                processing="Natural",
                varietal=["Heirloom", "74110"],
                altitude=1900,
                altitude_category="High",
                moisture=11.5,
                density="Medium",
                harvest_date="2024-01",
                purchase_date="2024-03-15",
                quantity_grams=5000,
                notes="Blueberry and wine notes",
            )

            assert result["id"] is not None
            assert result["name"] == "Yirgacheffe Natural"
            assert result["origin"] == "Ethiopia"
            assert result["processing"] == "Natural"
            assert result["varietal"] == ["Heirloom", "74110"]
            assert result["altitude"] == 1900
            assert result["altitude_category"] == "High"
            assert result["moisture"] == 11.5
            assert result["density"] == "Medium"
            assert result["harvest_date"] == "2024-01"
            assert result["purchase_date"] == "2024-03-15"
            assert result["quantity_grams"] == 0
            assert result["inventory_known"] is False
            assert (await Coffee.get(result["id"])).quantity_grams == 5000
            assert result["notes"] == "Blueberry and wine notes"

    @pytest.mark.asyncio
    async def test_add_coffee_persists(self) -> None:
        """Should persist the coffee to the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await add_coffee(ctx, name="Persisted Coffee", origin="Colombia")
            coffee_id = result["id"]

            # Verify it's in the database
            repo = CoffeeRepository()
            coffee = await repo.get_by_id(coffee_id)

            assert coffee is not None
            assert coffee.name == "Persisted Coffee"
            assert coffee.origin == "Colombia"


# =============================================================================
# list_coffees Tests
# =============================================================================


class TestListCoffees:
    """Tests for list_coffees tool."""

    @pytest.mark.asyncio
    async def test_list_coffees_empty(self) -> None:
        """Should return empty list when no coffees exist."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await list_coffees(ctx)

            assert result["coffees"] == []
            assert result["total"] == 0
            assert result["limit"] == 50
            assert result["offset"] == 0

    @pytest.mark.asyncio
    async def test_list_coffees_returns_summaries(self) -> None:
        """Should return coffee summaries with basic info."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Add some coffees
            await add_coffee(ctx, name="Coffee A", origin="Ethiopia", processing="Washed")
            await add_coffee(ctx, name="Coffee B", origin="Colombia", processing="Natural")

            result = await list_coffees(ctx)

            assert result["total"] == 2
            assert len(result["coffees"]) == 2

            # Check summary format (should have id, name, origin, processing)
            summaries = result["coffees"]
            for summary in summaries:
                assert "id" in summary
                assert "name" in summary
                assert "origin" in summary
                assert "processing" in summary

    @pytest.mark.asyncio
    async def test_list_coffees_filter_by_origin(self) -> None:
        """Should filter coffees by origin."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            await add_coffee(ctx, name="Ethiopia 1", origin="Ethiopia")
            await add_coffee(ctx, name="Ethiopia 2", origin="Ethiopia Yirgacheffe")
            await add_coffee(ctx, name="Colombia 1", origin="Colombia")

            result = await list_coffees(ctx, origin_filter="Ethiopia")

            assert result["total"] == 2
            assert len(result["coffees"]) == 2
            for coffee in result["coffees"]:
                assert "Ethiopia" in coffee["origin"]

    @pytest.mark.asyncio
    async def test_list_coffees_filter_by_processing(self) -> None:
        """Should filter coffees by processing method."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            await add_coffee(ctx, name="Natural 1", origin="Ethiopia", processing="Natural")
            await add_coffee(ctx, name="Natural 2", origin="Brazil", processing="Natural")
            await add_coffee(ctx, name="Washed 1", origin="Colombia", processing="Washed")

            result = await list_coffees(ctx, processing_filter="Natural")

            assert len(result["coffees"]) == 2
            for coffee in result["coffees"]:
                assert coffee["processing"] == "Natural"

    @pytest.mark.asyncio
    async def test_list_coffees_pagination(self) -> None:
        """Should support pagination with limit and offset."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Add 5 coffees
            for i in range(5):
                await add_coffee(ctx, name=f"Coffee {i}", origin="Test")

            # Get first page
            page1 = await list_coffees(ctx, limit=2, offset=0)
            assert len(page1["coffees"]) == 2
            assert page1["total"] == 5

            # Get second page
            page2 = await list_coffees(ctx, limit=2, offset=2)
            assert len(page2["coffees"]) == 2
            assert page2["offset"] == 2

            # Get last page (partial)
            page3 = await list_coffees(ctx, limit=2, offset=4)
            assert len(page3["coffees"]) == 1

    @pytest.mark.asyncio
    async def test_list_coffees_limit_clamped(self) -> None:
        """Should clamp limit to max 100."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await list_coffees(ctx, limit=500)

            assert result["limit"] == 100


# =============================================================================
# get_coffee Tests
# =============================================================================


class TestGetCoffee:
    """Tests for get_coffee tool."""

    @pytest.mark.asyncio
    async def test_get_coffee_returns_full_details(self) -> None:
        """Should return full coffee details including all fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Add a coffee with all fields
            added = await add_coffee(
                ctx,
                name="Test Coffee",
                origin="Ethiopia",
                processing="Natural",
                varietal=["Heirloom"],
                altitude=1800,
                notes="Test notes",
            )

            result = await get_coffee(ctx, added["id"])

            assert result["id"] == added["id"]
            assert result["name"] == "Test Coffee"
            assert result["origin"] == "Ethiopia"
            assert result["processing"] == "Natural"
            assert result["varietal"] == ["Heirloom"]
            assert result["altitude"] == 1800
            assert result["notes"] == "Test notes"
            assert result["created_at"] is not None
            assert result["updated_at"] is not None

    @pytest.mark.asyncio
    async def test_get_coffee_not_found(self) -> None:
        """Should raise ValueError when coffee not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Coffee not found"):
                await get_coffee(ctx, "non-existent-uuid")


# =============================================================================
# update_coffee Tests
# =============================================================================


class TestUpdateCoffee:
    """Tests for update_coffee tool."""

    @pytest.mark.asyncio
    async def test_update_coffee_single_field(self) -> None:
        """Should update a single field."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="Original Name", origin="Ethiopia")

            result = await update_coffee(ctx, added["id"], name="Updated Name")

            assert result["name"] == "Updated Name"
            assert result["origin"] == "Ethiopia"  # Unchanged

    @pytest.mark.asyncio
    async def test_update_coffee_multiple_fields(self) -> None:
        """Should update multiple fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="Test", origin="Ethiopia")

            result = await update_coffee(
                ctx,
                added["id"],
                name="Updated Name",
                processing="Washed",
                altitude=1700,
                notes="New notes",
            )

            assert result["name"] == "Updated Name"
            assert result["processing"] == "Washed"
            assert result["altitude"] == 1700
            assert result["notes"] == "New notes"

    @pytest.mark.asyncio
    async def test_update_coffee_varietal(self) -> None:
        """Should update varietal list."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="Test", origin="Kenya")

            result = await update_coffee(ctx, added["id"], varietal=["SL28", "SL34"])

            assert result["varietal"] == ["SL28", "SL34"]

    @pytest.mark.asyncio
    async def test_update_coffee_not_found(self) -> None:
        """Should raise ValueError when coffee not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Coffee not found"):
                await update_coffee(ctx, "non-existent-uuid", name="New Name")

    @pytest.mark.asyncio
    async def test_update_coffee_no_fields_provided(self) -> None:
        """Should raise ValueError when no fields to update."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="Test", origin="Ethiopia")

            with pytest.raises(ValueError, match="No fields provided"):
                await update_coffee(ctx, added["id"])

    @pytest.mark.asyncio
    async def test_update_coffee_persists(self) -> None:
        """Should persist updates to the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="Original", origin="Ethiopia")
            await update_coffee(ctx, added["id"], name="Updated")

            # Fetch fresh from database
            fetched = await get_coffee(ctx, added["id"])
            assert fetched["name"] == "Updated"


# =============================================================================
# delete_coffee Tests
# =============================================================================


class TestDeleteCoffee:
    """Tests for delete_coffee tool."""

    @pytest.mark.asyncio
    async def test_delete_coffee_success(self) -> None:
        """Should delete a coffee and return confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="To Delete", origin="Ethiopia")

            result = await delete_coffee(ctx, added["id"])

            assert result["deleted"] is True
            assert result["coffee_id"] == added["id"]

    @pytest.mark.asyncio
    async def test_delete_coffee_removes_from_database(self) -> None:
        """Should remove the coffee from the database."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            added = await add_coffee(ctx, name="To Delete", origin="Ethiopia")
            await delete_coffee(ctx, added["id"])

            # Verify it's gone
            with pytest.raises(ValueError, match="Coffee not found"):
                await get_coffee(ctx, added["id"])

    @pytest.mark.asyncio
    async def test_delete_coffee_not_found(self) -> None:
        """Should raise ValueError when coffee not found."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Coffee not found"):
                await delete_coffee(ctx, "non-existent-uuid")


# =============================================================================
# Integration Tests
# =============================================================================


class TestCoffeeToolsIntegration:
    """Integration tests for coffee tools working together."""

    @pytest.mark.asyncio
    async def test_full_crud_workflow(self) -> None:
        """Test a complete CRUD workflow."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Create
            created = await add_coffee(
                ctx,
                name="Integration Test Coffee",
                origin="Brazil",
                processing="Natural",
                altitude=1200,
            )
            coffee_id = created["id"]
            assert coffee_id is not None

            # Read
            retrieved = await get_coffee(ctx, coffee_id)
            assert retrieved["name"] == "Integration Test Coffee"

            # List (should include our coffee)
            listed = await list_coffees(ctx)
            assert any(c["id"] == coffee_id for c in listed["coffees"])

            # Update
            updated = await update_coffee(
                ctx,
                coffee_id,
                name="Updated Integration Test Coffee",
                notes="Added during integration test",
            )
            assert updated["name"] == "Updated Integration Test Coffee"
            assert updated["notes"] == "Added during integration test"
            assert updated["origin"] == "Brazil"  # Unchanged

            # Verify update persisted
            retrieved_again = await get_coffee(ctx, coffee_id)
            assert retrieved_again["name"] == "Updated Integration Test Coffee"

            # Delete
            delete_result = await delete_coffee(ctx, coffee_id)
            assert delete_result["deleted"] is True

            # Verify deleted
            with pytest.raises(ValueError, match="Coffee not found"):
                await get_coffee(ctx, coffee_id)

            # List should no longer include it
            final_list = await list_coffees(ctx)
            assert not any(c["id"] == coffee_id for c in final_list["coffees"])
