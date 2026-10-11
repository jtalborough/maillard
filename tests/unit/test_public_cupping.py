"""Tests for the public cupping intake form routes."""

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from maillard.db.repository import (
    CoffeeRepository,
    CuppingRepository,
    ProfileRepository,
    RoastRepository,
)
from maillard.db.schemas import (
    CoffeeCreate,
    PhaseSchema,
    ProfileCreate,
    RoastCreate,
)


@pytest.fixture
async def app_client(mongo_db):
    """Create a test FastAPI client with public routes."""
    from fastapi import FastAPI

    from maillard.web.public_routes import create_public_router
    from maillard.web.routes import create_router

    app = FastAPI()

    mock_state = MagicMock()

    app.state.maillard = mock_state
    app.state.ai_agent = None
    app.include_router(create_router())
    app.include_router(create_public_router())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create_test_roast(mongo_db) -> tuple[str, str]:
    """Create a coffee + completed roast, return (roast_id, coffee_id)."""
    coffee_repo = CoffeeRepository()
    coffee = await coffee_repo.create(
        CoffeeCreate(name="Ethiopia Yirgacheffe", origin="Ethiopia", processing="Washed")
    )

    profile_repo = ProfileRepository()
    profile = await profile_repo.create(
        ProfileCreate(
            name="Light Filter",
            roast_level="Light",
            phases=[PhaseSchema(
                name="charge", target_time_seconds=0, target_temp=200,
                heat_percent=100, fan_percent=40, drum_percent=60,
            )],
        )
    )

    roast_repo = RoastRepository()
    roast = await roast_repo.create(
        RoastCreate(coffee_id=coffee.id, profile_id=profile.id)
    )
    # Set start_time directly on the model (not in RoastUpdate schema)
    roast.start_time = "2026-02-15T10:00:00"
    await roast.save()
    await roast_repo.complete(
        roast.id, end_time="2026-02-15T10:12:00", total_time_seconds=720
    )
    return roast.id, coffee.id


# =============================================================================
# GET /cup/{roast_id} — HTML form page
# =============================================================================


class TestCuppingFormPage:
    @pytest.mark.asyncio
    async def test_returns_html(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        resp = await app_client.get(f"/cup/{roast_id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_contains_form_elements(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        resp = await app_client.get(f"/cup/{roast_id}")
        html = resp.text
        assert "cupping-form" in html
        assert 'name="aroma"' in html
        assert 'name="acidity"' in html
        assert 'name="sweetness"' in html
        assert 'name="body"' in html
        assert 'name="aftertaste"' in html
        assert 'name="overall"' in html
        assert 'name="flavor_notes"' in html
        assert 'name="brew_method"' in html
        assert 'name="cupper_name"' in html

    @pytest.mark.asyncio
    async def test_mobile_viewport_meta(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        resp = await app_client.get(f"/cup/{roast_id}")
        assert "viewport" in resp.text
        assert "width=device-width" in resp.text

    @pytest.mark.asyncio
    async def test_404_for_missing_roast(self, app_client: AsyncClient) -> None:
        resp = await app_client.get("/cup/nonexistent-roast-id")
        assert resp.status_code == 404
        assert "Not Found" in resp.text


# =============================================================================
# GET /api/public/roast/{roast_id} — roast info JSON
# =============================================================================


class TestPublicRoastInfo:
    @pytest.mark.asyncio
    async def test_returns_roast_info(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        resp = await app_client.get(f"/api/public/roast/{roast_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["coffee_name"] == "Ethiopia Yirgacheffe"
        assert data["origin"] == "Ethiopia"
        assert data["processing"] == "Washed"
        assert data["roast_level"] == "Light"
        assert data["roast_date"] == "2026-02-15"

    @pytest.mark.asyncio
    async def test_404_for_missing_roast(self, app_client: AsyncClient) -> None:
        resp = await app_client.get("/api/public/roast/nonexistent-id")
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_handles_no_coffee(self, mongo_db, app_client: AsyncClient) -> None:
        """Roast with no coffee_id should return nulls for coffee fields."""
        roast_repo = RoastRepository()
        roast = await roast_repo.create(RoastCreate())

        resp = await app_client.get(f"/api/public/roast/{roast.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["coffee_name"] is None
        assert data["origin"] is None


# =============================================================================
# POST /api/public/cuppings/{roast_id} — submission
# =============================================================================


class TestPublicCuppingSubmission:
    @pytest.mark.asyncio
    async def test_submit_cupping(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        payload = {
            "aroma": 8.0,
            "acidity": 7.5,
            "sweetness": 8.5,
            "body": 7.0,
            "aftertaste": 7.5,
            "overall": 8.0,
            "flavor_notes": "citrus, floral",
            "brew_method": "V60",
            "cupper_name": "Alice",
            "rest_days": 7,
        }
        resp = await app_client.post(f"/api/public/cuppings/{roast_id}", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["roast_id"] == roast_id
        assert data["aroma"] == 8.0
        assert data["acidity"] == 7.5
        assert data["cupper_name"] == "Alice"
        assert data["brew_method"] == "V60"

    @pytest.mark.asyncio
    async def test_creates_cupping_record(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        payload = {"aroma": 8.0, "cupper_name": "Bob"}
        resp = await app_client.post(f"/api/public/cuppings/{roast_id}", json=payload)
        assert resp.status_code == 201

        # Verify record exists in DB
        cupping_repo = CuppingRepository()
        cuppings = await cupping_repo.get_by_roast_id(roast_id)
        assert len(cuppings) == 1
        assert cuppings[0].aroma == 8.0
        assert cuppings[0].cupper_name == "Bob"

    @pytest.mark.asyncio
    async def test_response_includes_aggregation(self, mongo_db, app_client: AsyncClient) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        payload = {"aroma": 8.0, "overall": 7.5}
        resp = await app_client.post(f"/api/public/cuppings/{roast_id}", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert "aggregation" in data
        agg = data["aggregation"]
        assert agg["count"] == 1
        assert agg["avg_total"] is not None
        assert len(agg["dimension_averages"]) > 0

    @pytest.mark.asyncio
    async def test_404_for_missing_roast(self, app_client: AsyncClient) -> None:
        resp = await app_client.post(
            "/api/public/cuppings/nonexistent-id",
            json={"aroma": 7.0},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_flavor_notes_parsed_from_comma_string(
        self, mongo_db, app_client: AsyncClient
    ) -> None:
        roast_id, _ = await _create_test_roast(mongo_db)
        payload = {"flavor_notes": "citrus, honey, chocolate"}
        resp = await app_client.post(f"/api/public/cuppings/{roast_id}", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["flavor_notes"] == ["citrus", "honey", "chocolate"]

    @pytest.mark.asyncio
    async def test_empty_submission_allowed(
        self, mongo_db, app_client: AsyncClient
    ) -> None:
        """A submission with no scores is still valid (e.g., just notes)."""
        roast_id, _ = await _create_test_roast(mongo_db)
        resp = await app_client.post(f"/api/public/cuppings/{roast_id}", json={})
        assert resp.status_code == 201


# =============================================================================
# Rate limiting
# =============================================================================


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_triggers(self, mongo_db, app_client: AsyncClient) -> None:
        """After 10 submissions, the 11th should be rejected."""
        from maillard.web.public_routes import _rate_limit_store

        # Clear any existing rate limit data
        _rate_limit_store.clear()

        roast_id, _ = await _create_test_roast(mongo_db)

        # Submit 10 times (should all succeed)
        for i in range(10):
            resp = await app_client.post(
                f"/api/public/cuppings/{roast_id}",
                json={"aroma": 7.0 + i * 0.1},
            )
            assert resp.status_code == 201, f"Submission {i+1} failed"

        # 11th should be rate-limited
        resp = await app_client.post(
            f"/api/public/cuppings/{roast_id}",
            json={"aroma": 8.5},
        )
        assert resp.status_code == 429
        assert "rate limit" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_rate_limit_per_roast(self, mongo_db, app_client: AsyncClient) -> None:
        """Rate limit is per-roast, so different roasts have separate counters."""
        from maillard.web.public_routes import _rate_limit_store

        _rate_limit_store.clear()

        roast_id_1, _ = await _create_test_roast(mongo_db)

        # Create a second roast
        roast_repo = RoastRepository()
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Colombia", origin="Colombia"))
        roast2 = await roast_repo.create(RoastCreate(coffee_id=coffee.id))
        await roast_repo.complete(roast2.id, end_time="2026-02-15T11:00:00", total_time_seconds=600)
        roast_id_2 = roast2.id

        # Max out rate limit on roast 1
        for _ in range(10):
            await app_client.post(f"/api/public/cuppings/{roast_id_1}", json={"aroma": 7.0})

        # Should still be able to submit to roast 2
        resp = await app_client.post(f"/api/public/cuppings/{roast_id_2}", json={"aroma": 7.0})
        assert resp.status_code == 201
