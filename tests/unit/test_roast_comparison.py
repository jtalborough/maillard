"""Tests for roast comparison: repository queries and API routes."""

import pytest

from maillard.db.models import RoastStatus
from maillard.db.repository import (
    CoffeeRepository,
    CuppingRepository,
    RoastRepository,
)
from maillard.db.schemas import (
    CoffeeCreate,
    CuppingCreate,
    CuppingScores,
    RoastCreate,
    RoastUpdate,
)


# =============================================================================
# RoastRepository.get_completed_by_coffee_id
# =============================================================================


class TestGetCompletedByCoffeeId:
    """Tests for RoastRepository.get_completed_by_coffee_id."""

    async def _create_coffee(self, mongo_db, name: str = "Test") -> str:
        repo = CoffeeRepository()
        coffee = await repo.create(CoffeeCreate(name=name, origin="Ethiopia"))
        return coffee.id

    async def _create_completed_roast(
        self, mongo_db, coffee_id: str, start_time: str = "2025-01-15T10:00:00"
    ) -> str:
        repo = RoastRepository()
        roast = await repo.create(RoastCreate(coffee_id=coffee_id, batch_size_grams=450))
        await repo.update(roast.id, RoastUpdate(start_time=start_time))
        await repo.complete(roast.id, end_time="2025-01-15T10:10:00", total_time_seconds=600)
        return roast.id

    @pytest.mark.asyncio
    async def test_returns_only_completed(self, mongo_db) -> None:
        coffee_id = await self._create_coffee(mongo_db)
        repo = RoastRepository()

        # Create 1 completed, 1 in-progress, 1 aborted
        await self._create_completed_roast(mongo_db, coffee_id)
        await repo.create(RoastCreate(coffee_id=coffee_id, batch_size_grams=450))  # in-progress
        aborted = await repo.create(RoastCreate(coffee_id=coffee_id, batch_size_grams=450))
        await repo.abort(aborted.id, end_time="2025-01-15T10:05:00")

        result = await repo.get_completed_by_coffee_id(coffee_id)
        assert len(result) == 1
        assert all(r.status == RoastStatus.COMPLETED for r in result)

    @pytest.mark.asyncio
    async def test_returns_only_matching_coffee(self, mongo_db) -> None:
        coffee_a = await self._create_coffee(mongo_db, "Coffee A")
        coffee_b = await self._create_coffee(mongo_db, "Coffee B")

        await self._create_completed_roast(mongo_db, coffee_a)
        await self._create_completed_roast(mongo_db, coffee_a)
        await self._create_completed_roast(mongo_db, coffee_b)

        repo = RoastRepository()
        result = await repo.get_completed_by_coffee_id(coffee_a)
        assert len(result) == 2
        assert all(r.coffee_id == coffee_a for r in result)

    @pytest.mark.asyncio
    async def test_respects_limit(self, mongo_db) -> None:
        coffee_id = await self._create_coffee(mongo_db)
        for i in range(5):
            await self._create_completed_roast(
                mongo_db, coffee_id, start_time=f"2025-01-{15 + i}T10:00:00"
            )

        repo = RoastRepository()
        result = await repo.get_completed_by_coffee_id(coffee_id, limit=3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_excludes_specified_roast_id(self, mongo_db) -> None:
        coffee_id = await self._create_coffee(mongo_db)
        r1 = await self._create_completed_roast(
            mongo_db, coffee_id, start_time="2025-01-15T10:00:00"
        )
        r2 = await self._create_completed_roast(
            mongo_db, coffee_id, start_time="2025-01-16T10:00:00"
        )

        repo = RoastRepository()
        result = await repo.get_completed_by_coffee_id(
            coffee_id, exclude_roast_id=r1
        )
        assert len(result) == 1
        assert result[0].id == r2

    @pytest.mark.asyncio
    async def test_cuppings_loaded_separately(self, mongo_db) -> None:
        """Cuppings are loaded via separate query (no ORM eager loading)."""
        coffee_id = await self._create_coffee(mongo_db)
        roast_id = await self._create_completed_roast(mongo_db, coffee_id)

        cupping_repo = CuppingRepository()
        await cupping_repo.create(
            CuppingCreate(
                roast_id=roast_id,
                scores=CuppingScores(aroma=8.0, overall=7.5),
                flavor_notes=["citrus"],
            )
        )

        # Cuppings are loaded separately in MongoDB (no ORM relationships)
        cuppings = await cupping_repo.get_by_roast_id(roast_id)
        assert len(cuppings) == 1
        assert cuppings[0].aroma == 8.0

    @pytest.mark.asyncio
    async def test_empty_when_no_completed(self, mongo_db) -> None:
        coffee_id = await self._create_coffee(mongo_db)
        repo = RoastRepository()
        # Only in-progress
        await repo.create(RoastCreate(coffee_id=coffee_id, batch_size_grams=450))

        result = await repo.get_completed_by_coffee_id(coffee_id)
        assert len(result) == 0


# =============================================================================
# API Route Tests — /api/roasts/compare and /api/roasts/{id}/similar
# =============================================================================


class TestCompareRoutes:
    """Tests for the comparison API routes."""

    @pytest.fixture
    async def app_client(self, mongo_db):
        """Create a test FastAPI client with a real DB."""
        from unittest.mock import MagicMock

        from httpx import ASGITransport, AsyncClient

        from maillard.web.routes import create_router

        # Build a minimal FastAPI app with routes
        from fastapi import FastAPI

        app = FastAPI()

        mock_state = MagicMock()
        app.state.maillard = mock_state
        app.state.ai_agent = None
        app.include_router(create_router())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    # ---- /api/roasts/compare ----

    @pytest.mark.asyncio
    async def test_compare_too_few_ids(self, app_client) -> None:
        resp = await app_client.get("/api/roasts/compare", params={"ids": "one-id"})
        data = resp.json()
        assert data["status"] == "error"
        assert "at least 2" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_compare_too_many_ids(self, app_client) -> None:
        ids = ",".join(f"id-{i}" for i in range(11))
        resp = await app_client.get("/api/roasts/compare", params={"ids": ids})
        data = resp.json()
        assert data["status"] == "error"
        assert "10" in data["error"]

    @pytest.mark.asyncio
    async def test_compare_missing_roast(self, app_client) -> None:
        resp = await app_client.get(
            "/api/roasts/compare", params={"ids": "fake-id-1,fake-id-2"}
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_compare_success(self, mongo_db, app_client) -> None:
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Ethiopia"))

        roast_repo = RoastRepository()
        r1 = await roast_repo.create(RoastCreate(coffee_id=coffee.id, batch_size_grams=450))
        await roast_repo.update(r1.id, RoastUpdate(
            start_time="2025-01-15T10:00:00",
            charge_temp=195.0, drop_temp=210.0, drop_time=600,
            total_time_seconds=600, dtr=20.0,
        ))
        await roast_repo.complete(r1.id, end_time="2025-01-15T10:10:00", total_time_seconds=600)

        r2 = await roast_repo.create(RoastCreate(coffee_id=coffee.id, batch_size_grams=450))
        await roast_repo.update(r2.id, RoastUpdate(
            start_time="2025-01-16T10:00:00",
            charge_temp=192.0, drop_temp=208.0, drop_time=580,
            total_time_seconds=580, dtr=19.0,
        ))
        await roast_repo.complete(r2.id, end_time="2025-01-16T10:10:00", total_time_seconds=580)

        resp = await app_client.get(
            "/api/roasts/compare", params={"ids": f"{r1.id},{r2.id}"}
        )
        data = resp.json()
        assert "roasts" in data
        assert len(data["roasts"]) == 2
        ids_returned = {r["roast_id"] for r in data["roasts"]}
        assert ids_returned == {r1.id, r2.id}
        # Verify key metrics present
        first = data["roasts"][0]
        assert "charge_temp" in first
        assert "drop_temp" in first
        assert "dtr" in first

    # ---- /api/roasts/{roast_id}/similar ----

    @pytest.mark.asyncio
    async def test_similar_missing_roast(self, app_client) -> None:
        resp = await app_client.get("/api/roasts/fake-id/similar")
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_similar_no_coffee_id(self, mongo_db, app_client) -> None:
        roast_repo = RoastRepository()
        roast = await roast_repo.create(RoastCreate())  # no coffee_id

        resp = await app_client.get(f"/api/roasts/{roast.id}/similar")
        data = resp.json()
        assert data["roasts"] == []
        assert "no associated coffee" in data.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_similar_returns_completed_excluding_self(
        self, mongo_db, app_client
    ) -> None:
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Ethiopia"))

        roast_repo = RoastRepository()
        # The "self" roast
        self_roast = await roast_repo.create(RoastCreate(coffee_id=coffee.id, batch_size_grams=450))
        await roast_repo.update(self_roast.id, RoastUpdate(start_time="2025-01-17T10:00:00"))
        await roast_repo.complete(
            self_roast.id, end_time="2025-01-17T10:10:00", total_time_seconds=600
        )

        # Another completed roast of same coffee
        other = await roast_repo.create(RoastCreate(coffee_id=coffee.id, batch_size_grams=450))
        await roast_repo.update(other.id, RoastUpdate(start_time="2025-01-16T10:00:00"))
        await roast_repo.complete(
            other.id, end_time="2025-01-16T10:10:00", total_time_seconds=590
        )

        resp = await app_client.get(f"/api/roasts/{self_roast.id}/similar")
        data = resp.json()
        assert "roasts" in data
        assert len(data["roasts"]) == 1
        assert data["roasts"][0]["roast_id"] == other.id
        assert data["coffee_id"] == coffee.id
