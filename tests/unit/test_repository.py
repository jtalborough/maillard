"""Tests for the repository layer."""

import pytest

from maillard.db.models import Coffee, Cupping, Profile, Roast, RoastStatus
from maillard.db.repository import (
    CoffeeRepository,
    CuppingRepository,
    ProfileRepository,
    RoastRepository,
)
from maillard.db.schemas import (
    CoffeeCreate,
    CoffeeUpdate,
    CuppingCreate,
    CuppingScores,
    CuppingUpdate,
    CurveDataSchema,
    PhaseSchema,
    ProfileCreate,
    ProfileUpdate,
    RoastCreate,
    RoastUpdate,
)


# =============================================================================
# CoffeeRepository Tests
# =============================================================================


class TestCoffeeRepository:
    """Tests for CoffeeRepository CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_coffee_minimal(self, mongo_db) -> None:
        """Should create a coffee with only required fields."""
        repo = CoffeeRepository()
        data = CoffeeCreate(name="Test Coffee", origin="Ethiopia")

        coffee = await repo.create(data)

        assert coffee.id is not None
        assert coffee.name == "Test Coffee"
        assert coffee.origin == "Ethiopia"
        assert coffee.processing is None
        assert coffee.created_at is not None

    @pytest.mark.asyncio
    async def test_create_coffee_full(self, mongo_db) -> None:
        """Should create a coffee with all fields."""
        repo = CoffeeRepository()
        data = CoffeeCreate(
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

        coffee = await repo.create(data)

        assert coffee.id is not None
        assert coffee.name == "Yirgacheffe Natural"
        assert coffee.processing == "Natural"
        # Varietal stored as native list
        assert coffee.varietal == ["Heirloom", "74110"]
        assert coffee.altitude == 1900
        assert coffee.moisture == 11.5

    @pytest.mark.asyncio
    async def test_get_by_id(self, mongo_db) -> None:
        """Should retrieve a coffee by ID."""
        repo = CoffeeRepository()
        data = CoffeeCreate(name="Kenya AA", origin="Kenya")
        created = await repo.create(data)

        result = await repo.get_by_id(created.id)

        assert result is not None
        assert result.id == created.id
        assert result.name == "Kenya AA"

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, mongo_db) -> None:
        """Should return None for non-existent ID."""
        repo = CoffeeRepository()

        result = await repo.get_by_id("non-existent-uuid")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_all(self, mongo_db) -> None:
        """Should retrieve all coffees with pagination."""
        repo = CoffeeRepository()
        # Create multiple coffees
        for i in range(5):
            await repo.create(CoffeeCreate(name=f"Coffee {i}", origin=f"Origin {i}"))

        result = await repo.get_all(limit=3, offset=0)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_all_pagination(self, mongo_db) -> None:
        """Should support offset pagination."""
        repo = CoffeeRepository()
        for i in range(5):
            await repo.create(CoffeeCreate(name=f"Coffee {i}", origin="Test"))

        first_page = await repo.get_all(limit=2, offset=0)
        second_page = await repo.get_all(limit=2, offset=2)

        assert len(first_page) == 2
        assert len(second_page) == 2
        # Ensure no overlap (checking by id)
        first_ids = {c.id for c in first_page}
        second_ids = {c.id for c in second_page}
        assert first_ids.isdisjoint(second_ids)

    @pytest.mark.asyncio
    async def test_get_by_origin(self, mongo_db) -> None:
        """Should filter coffees by origin."""
        repo = CoffeeRepository()
        await repo.create(CoffeeCreate(name="Ethiopian 1", origin="Ethiopia"))
        await repo.create(CoffeeCreate(name="Ethiopian 2", origin="Ethiopia, Sidamo"))
        await repo.create(CoffeeCreate(name="Kenyan", origin="Kenya"))

        result = await repo.get_by_origin("Ethiopia")

        assert len(result) == 2
        assert all("Ethiopia" in c.origin for c in result)

    @pytest.mark.asyncio
    async def test_get_by_processing(self, mongo_db) -> None:
        """Should filter coffees by processing method."""
        repo = CoffeeRepository()
        await repo.create(CoffeeCreate(name="Natural 1", origin="Ethiopia", processing="Natural"))
        await repo.create(CoffeeCreate(name="Natural 2", origin="Brazil", processing="Natural"))
        await repo.create(CoffeeCreate(name="Washed", origin="Kenya", processing="Washed"))

        result = await repo.get_by_processing("Natural")

        assert len(result) == 2
        assert all(c.processing == "Natural" for c in result)

    @pytest.mark.asyncio
    async def test_update_coffee(self, mongo_db) -> None:
        """Should update a coffee entry."""
        repo = CoffeeRepository()
        created = await repo.create(CoffeeCreate(name="Original", origin="Brazil"))

        updated = await repo.update(
            created.id,
            CoffeeUpdate(name="Updated Name", notes="New notes"),
        )

        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.notes == "New notes"
        assert updated.origin == "Brazil"  # Unchanged

    @pytest.mark.asyncio
    async def test_update_coffee_varietal(self, mongo_db) -> None:
        """Should update varietal as native list."""
        repo = CoffeeRepository()
        created = await repo.create(CoffeeCreate(name="Test", origin="Colombia"))

        updated = await repo.update(
            created.id,
            CoffeeUpdate(varietal=["Caturra", "Castillo"]),
        )

        assert updated is not None
        assert updated.varietal == ["Caturra", "Castillo"]

    @pytest.mark.asyncio
    async def test_update_coffee_not_found(self, mongo_db) -> None:
        """Should return None when updating non-existent coffee."""
        repo = CoffeeRepository()

        result = await repo.update("non-existent", CoffeeUpdate(name="New"))

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_coffee(self, mongo_db) -> None:
        """Should delete a coffee entry."""
        repo = CoffeeRepository()
        created = await repo.create(CoffeeCreate(name="To Delete", origin="Test"))

        result = await repo.delete(created.id)

        assert result is True
        assert await repo.get_by_id(created.id) is None

    @pytest.mark.asyncio
    async def test_delete_coffee_not_found(self, mongo_db) -> None:
        """Should return False when deleting non-existent coffee."""
        repo = CoffeeRepository()

        result = await repo.delete("non-existent")

        assert result is False

    @pytest.mark.asyncio
    async def test_count(self, mongo_db) -> None:
        """Should count total coffees."""
        repo = CoffeeRepository()
        for i in range(3):
            await repo.create(CoffeeCreate(name=f"Coffee {i}", origin="Test"))

        count = await repo.count()

        assert count == 3


# =============================================================================
# ProfileRepository Tests
# =============================================================================


def _sample_phases() -> list[PhaseSchema]:
    """Create sample phases for testing."""
    return [
        PhaseSchema(
            name="charge",
            target_time_seconds=0,
            target_temp=195.0,
            heat_percent=100,
            fan_percent=40,
            drum_percent=60,
        ),
        PhaseSchema(
            name="dry_end",
            target_time_seconds=240,
            target_temp=155.0,
            heat_percent=80,
            fan_percent=50,
            drum_percent=60,
        ),
        PhaseSchema(
            name="drop",
            target_time_seconds=540,
            target_temp=210.0,
            heat_percent=50,
            fan_percent=70,
            drum_percent=60,
        ),
    ]


class TestProfileRepository:
    """Tests for ProfileRepository CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_profile_minimal(self, mongo_db) -> None:
        """Should create a profile with minimal fields."""
        repo = ProfileRepository()
        data = ProfileCreate(name="Test Profile", phases=_sample_phases())

        profile = await repo.create(data)

        assert profile.id is not None
        assert profile.name == "Test Profile"
        assert profile.version == 1
        # Phases stored as native list of dicts
        assert len(profile.phases) == 3
        assert profile.phases[0]["name"] == "charge"

    @pytest.mark.asyncio
    async def test_create_profile_full(self, mongo_db) -> None:
        """Should create a profile with all fields."""
        repo = ProfileRepository()
        data = ProfileCreate(
            name="Light Ethiopian",
            roast_level="Light",
            flavor_goal="Highlight floral notes",
            brew_method="Filter",
            batch_size_grams=500,
            phases=_sample_phases(),
            target_dtr=20.0,
            target_weight_loss=13.0,
            charge_temp=195.0,
            ai_rationale="Selected for Ethiopian coffee",
        )

        profile = await repo.create(data)

        assert profile.roast_level == "Light"
        assert profile.target_dtr == 20.0
        assert profile.ai_rationale == "Selected for Ethiopian coffee"

    @pytest.mark.asyncio
    async def test_create_profile_with_coffee(self, mongo_db) -> None:
        """Should create a profile linked to a coffee."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Ethiopia"))

        repo = ProfileRepository()
        data = ProfileCreate(
            name="Ethiopian Profile",
            coffee_id=coffee.id,
            phases=_sample_phases(),
        )

        profile = await repo.create(data)

        assert profile.coffee_id == coffee.id

    @pytest.mark.asyncio
    async def test_get_by_id(self, mongo_db) -> None:
        """Should retrieve a profile by ID."""
        repo = ProfileRepository()
        created = await repo.create(ProfileCreate(name="Test", phases=_sample_phases()))

        result = await repo.get_by_id(created.id)

        assert result is not None
        assert result.name == "Test"

    @pytest.mark.asyncio
    async def test_get_by_id_with_coffee(self, mongo_db) -> None:
        """Should retrieve profile with coffee_id set."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test Coffee", origin="Brazil"))

        repo = ProfileRepository()
        created = await repo.create(
            ProfileCreate(name="Test Profile", coffee_id=coffee.id, phases=_sample_phases())
        )

        result = await repo.get_by_id_with_coffee(created.id)

        assert result is not None
        assert result.coffee_id == coffee.id
        # In MongoDB, we load coffee separately (no ORM relationships)
        coffee_obj = await coffee_repo.get_by_id(result.coffee_id)
        assert coffee_obj is not None
        assert coffee_obj.name == "Test Coffee"

    @pytest.mark.asyncio
    async def test_get_all(self, mongo_db) -> None:
        """Should retrieve all profiles with pagination."""
        repo = ProfileRepository()
        for i in range(5):
            await repo.create(ProfileCreate(name=f"Profile {i}", phases=_sample_phases()))

        result = await repo.get_all(limit=3)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_by_coffee_id(self, mongo_db) -> None:
        """Should filter profiles by coffee ID."""
        coffee_repo = CoffeeRepository()
        coffee1 = await coffee_repo.create(CoffeeCreate(name="Coffee 1", origin="Ethiopia"))
        coffee2 = await coffee_repo.create(CoffeeCreate(name="Coffee 2", origin="Kenya"))

        repo = ProfileRepository()
        await repo.create(ProfileCreate(name="P1", coffee_id=coffee1.id, phases=_sample_phases()))
        await repo.create(ProfileCreate(name="P2", coffee_id=coffee1.id, phases=_sample_phases()))
        await repo.create(ProfileCreate(name="P3", coffee_id=coffee2.id, phases=_sample_phases()))

        result = await repo.get_by_coffee_id(coffee1.id)

        assert len(result) == 2
        assert all(p.coffee_id == coffee1.id for p in result)

    @pytest.mark.asyncio
    async def test_get_by_roast_level(self, mongo_db) -> None:
        """Should filter profiles by roast level."""
        repo = ProfileRepository()
        await repo.create(ProfileCreate(name="Light 1", roast_level="Light", phases=_sample_phases()))
        await repo.create(ProfileCreate(name="Light 2", roast_level="Light", phases=_sample_phases()))
        await repo.create(ProfileCreate(name="Medium", roast_level="Medium", phases=_sample_phases()))

        result = await repo.get_by_roast_level("Light")

        assert len(result) == 2
        assert all(p.roast_level == "Light" for p in result)

    @pytest.mark.asyncio
    async def test_get_by_brew_method(self, mongo_db) -> None:
        """Should filter profiles by brew method."""
        repo = ProfileRepository()
        await repo.create(ProfileCreate(name="Filter 1", brew_method="Filter", phases=_sample_phases()))
        await repo.create(ProfileCreate(name="Espresso", brew_method="Espresso", phases=_sample_phases()))

        result = await repo.get_by_brew_method("Filter")

        assert len(result) == 1
        assert result[0].brew_method == "Filter"

    @pytest.mark.asyncio
    async def test_update_profile(self, mongo_db) -> None:
        """Should update a profile and increment version."""
        repo = ProfileRepository()
        created = await repo.create(ProfileCreate(name="Original", phases=_sample_phases()))
        original_version = created.version

        updated = await repo.update(
            created.id,
            ProfileUpdate(name="Updated", roast_level="Medium"),
        )

        assert updated is not None
        assert updated.name == "Updated"
        assert updated.roast_level == "Medium"
        assert updated.version == original_version + 1

    @pytest.mark.asyncio
    async def test_update_profile_phases(self, mongo_db) -> None:
        """Should update profile phases."""
        repo = ProfileRepository()
        created = await repo.create(ProfileCreate(name="Test", phases=_sample_phases()))

        new_phases = [
            PhaseSchema(
                name="charge",
                target_time_seconds=0,
                target_temp=200.0,
                heat_percent=100,
                fan_percent=35,
                drum_percent=65,
            ),
        ]
        updated = await repo.update(created.id, ProfileUpdate(phases=new_phases))

        assert updated is not None
        assert len(updated.phases) == 1
        assert updated.phases[0]["target_temp"] == 200.0

    @pytest.mark.asyncio
    async def test_delete_profile(self, mongo_db) -> None:
        """Should delete a profile."""
        repo = ProfileRepository()
        created = await repo.create(ProfileCreate(name="To Delete", phases=_sample_phases()))

        result = await repo.delete(created.id)

        assert result is True
        assert await repo.get_by_id(created.id) is None

    @pytest.mark.asyncio
    async def test_count(self, mongo_db) -> None:
        """Should count total profiles."""
        repo = ProfileRepository()
        for i in range(4):
            await repo.create(ProfileCreate(name=f"Profile {i}", phases=_sample_phases()))

        count = await repo.count()

        assert count == 4


# =============================================================================
# RoastRepository Tests
# =============================================================================


class TestRoastRepository:
    """Tests for RoastRepository CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_roast_minimal(self, mongo_db) -> None:
        """Should create a roast with minimal fields."""
        repo = RoastRepository()
        data = RoastCreate()

        roast = await repo.create(data)

        assert roast.id is not None
        assert roast.status == RoastStatus.IN_PROGRESS
        assert roast.profile_id is None
        assert roast.coffee_id is None

    @pytest.mark.asyncio
    async def test_create_roast_with_references(self, mongo_db) -> None:
        """Should create a roast with profile and coffee references."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Ethiopia"))

        profile_repo = ProfileRepository()
        profile = await profile_repo.create(ProfileCreate(name="Test", phases=_sample_phases()))

        repo = RoastRepository()
        data = RoastCreate(
            coffee_id=coffee.id,
            profile_id=profile.id,
            batch_size_grams=500,
            ambient_temp=22.0,
            ambient_humidity=50.0,
        )

        roast = await repo.create(data)

        assert roast.coffee_id == coffee.id
        assert roast.profile_id == profile.id
        assert roast.batch_size_grams == 500

    @pytest.mark.asyncio
    async def test_get_by_id(self, mongo_db) -> None:
        """Should retrieve a roast by ID."""
        repo = RoastRepository()
        created = await repo.create(RoastCreate())

        result = await repo.get_by_id(created.id)

        assert result is not None
        assert result.id == created.id

    @pytest.mark.asyncio
    async def test_get_by_id_with_relations(self, mongo_db) -> None:
        """Should retrieve roast with references set."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test Coffee", origin="Kenya"))

        profile_repo = ProfileRepository()
        profile = await profile_repo.create(ProfileCreate(name="Test Profile", phases=_sample_phases()))

        repo = RoastRepository()
        created = await repo.create(
            RoastCreate(coffee_id=coffee.id, profile_id=profile.id)
        )

        result = await repo.get_by_id_with_relations(created.id)

        assert result is not None
        assert result.coffee_id == coffee.id
        assert result.profile_id == profile.id
        # In MongoDB, load related objects separately
        coffee_obj = await coffee_repo.get_by_id(result.coffee_id)
        assert coffee_obj is not None
        assert coffee_obj.name == "Test Coffee"
        profile_obj = await profile_repo.get_by_id(result.profile_id)
        assert profile_obj is not None
        assert profile_obj.name == "Test Profile"

    @pytest.mark.asyncio
    async def test_get_all(self, mongo_db) -> None:
        """Should retrieve all roasts with pagination."""
        repo = RoastRepository()
        for _ in range(5):
            await repo.create(RoastCreate())

        result = await repo.get_all(limit=3)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_by_coffee_id(self, mongo_db) -> None:
        """Should filter roasts by coffee ID."""
        coffee_repo = CoffeeRepository()
        coffee1 = await coffee_repo.create(CoffeeCreate(name="Coffee 1", origin="Ethiopia"))
        coffee2 = await coffee_repo.create(CoffeeCreate(name="Coffee 2", origin="Kenya"))

        repo = RoastRepository()
        await repo.create(RoastCreate(coffee_id=coffee1.id))
        await repo.create(RoastCreate(coffee_id=coffee1.id))
        await repo.create(RoastCreate(coffee_id=coffee2.id))

        result = await repo.get_by_coffee_id(coffee1.id)

        assert len(result) == 2
        assert all(r.coffee_id == coffee1.id for r in result)

    @pytest.mark.asyncio
    async def test_get_by_profile_id(self, mongo_db) -> None:
        """Should filter roasts by profile ID."""
        profile_repo = ProfileRepository()
        profile1 = await profile_repo.create(ProfileCreate(name="Profile 1", phases=_sample_phases()))
        profile2 = await profile_repo.create(ProfileCreate(name="Profile 2", phases=_sample_phases()))

        repo = RoastRepository()
        await repo.create(RoastCreate(profile_id=profile1.id))
        await repo.create(RoastCreate(profile_id=profile2.id))
        await repo.create(RoastCreate(profile_id=profile2.id))

        result = await repo.get_by_profile_id(profile2.id)

        assert len(result) == 2
        assert all(r.profile_id == profile2.id for r in result)

    @pytest.mark.asyncio
    async def test_get_by_status(self, mongo_db) -> None:
        """Should filter roasts by status."""
        repo = RoastRepository()
        r1 = await repo.create(RoastCreate())
        r2 = await repo.create(RoastCreate())
        r3 = await repo.create(RoastCreate())

        # Complete some roasts
        await repo.complete(r1.id, "2024-01-01T12:00:00", 600)
        await repo.abort(r2.id, "2024-01-01T12:00:00")

        in_progress = await repo.get_by_status(RoastStatus.IN_PROGRESS)
        completed = await repo.get_by_status(RoastStatus.COMPLETED)
        aborted = await repo.get_by_status(RoastStatus.ABORTED)

        assert len(in_progress) == 1
        assert len(completed) == 1
        assert len(aborted) == 1

    @pytest.mark.asyncio
    async def test_get_in_progress(self, mongo_db) -> None:
        """Should retrieve the in-progress roast."""
        repo = RoastRepository()
        await repo.create(RoastCreate())

        result = await repo.get_in_progress()

        assert result is not None
        assert result.status == RoastStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_get_in_progress_none(self, mongo_db) -> None:
        """Should return None when no roast is in progress."""
        repo = RoastRepository()
        roast = await repo.create(RoastCreate())
        await repo.complete(roast.id, "2024-01-01T12:00:00", 600)

        result = await repo.get_in_progress()

        assert result is None

    @pytest.mark.asyncio
    async def test_update_roast(self, mongo_db) -> None:
        """Should update a roast."""
        repo = RoastRepository()
        created = await repo.create(RoastCreate())

        updated = await repo.update(
            created.id,
            RoastUpdate(
                charge_temp=195.0,
                turning_point_time=75,
                turning_point_temp=97.0,
            ),
        )

        assert updated is not None
        assert updated.charge_temp == 195.0
        assert updated.turning_point_time == 75

    @pytest.mark.asyncio
    async def test_update_curve_data(self, mongo_db) -> None:
        """Should update curve data as native subdocument."""
        repo = RoastRepository()
        created = await repo.create(RoastCreate())

        curve = CurveDataSchema(
            time_seconds=[0, 1.5, 3.0],
            bt=[195.0, 180.0, 165.0],
            et=[220.0, 215.0, 210.0],
            bt_ror=[None, -20.0, -15.0],
            heat=[100, 100, 100],
            fan=[40, 40, 45],
            drum=[60, 60, 60],
            events=[],
        )
        updated = await repo.update_curve_data(created.id, curve)

        assert updated is not None
        assert updated.curve_data is not None
        assert updated.curve_data["bt"] == [195.0, 180.0, 165.0]

    @pytest.mark.asyncio
    async def test_complete_roast(self, mongo_db) -> None:
        """Should mark a roast as completed."""
        repo = RoastRepository()
        created = await repo.create(RoastCreate())

        completed = await repo.complete(
            created.id,
            end_time="2024-01-01T12:10:00",
            total_time_seconds=600,
            dtr=25.0,
            weight_loss=13.5,
        )

        assert completed is not None
        assert completed.status == RoastStatus.COMPLETED
        assert completed.end_time == "2024-01-01T12:10:00"
        assert completed.total_time_seconds == 600
        assert completed.dtr == 25.0
        assert completed.weight_loss == 13.5

    @pytest.mark.asyncio
    async def test_abort_roast(self, mongo_db) -> None:
        """Should mark a roast as aborted."""
        repo = RoastRepository()
        created = await repo.create(RoastCreate())

        aborted = await repo.abort(
            created.id,
            end_time="2024-01-01T12:05:00",
            notes="Smoke detected",
        )

        assert aborted is not None
        assert aborted.status == RoastStatus.ABORTED
        assert aborted.end_time == "2024-01-01T12:05:00"
        assert aborted.notes == "Smoke detected"

    @pytest.mark.asyncio
    async def test_delete_roast(self, mongo_db) -> None:
        """Should delete a roast."""
        repo = RoastRepository()
        created = await repo.create(RoastCreate())

        result = await repo.delete(created.id)

        assert result is True
        assert await repo.get_by_id(created.id) is None

    @pytest.mark.asyncio
    async def test_count(self, mongo_db) -> None:
        """Should count total roasts."""
        repo = RoastRepository()
        for _ in range(3):
            await repo.create(RoastCreate())

        count = await repo.count()

        assert count == 3

    @pytest.mark.asyncio
    async def test_count_by_status(self, mongo_db) -> None:
        """Should count roasts filtered by status."""
        repo = RoastRepository()
        r1 = await repo.create(RoastCreate())
        r2 = await repo.create(RoastCreate())
        await repo.create(RoastCreate())

        await repo.complete(r1.id, "2024-01-01T12:00:00", 600)
        await repo.complete(r2.id, "2024-01-01T12:00:00", 600)

        completed_count = await repo.count(status=RoastStatus.COMPLETED)
        in_progress_count = await repo.count(status=RoastStatus.IN_PROGRESS)

        assert completed_count == 2
        assert in_progress_count == 1


# =============================================================================
# CuppingRepository Tests
# =============================================================================


class TestCuppingRepository:
    """Tests for CuppingRepository CRUD operations."""

    async def _create_roast(self, mongo_db) -> Roast:
        """Helper to create a roast for cupping tests."""
        repo = RoastRepository()
        return await repo.create(RoastCreate())

    @pytest.mark.asyncio
    async def test_create_cupping_minimal(self, mongo_db) -> None:
        """Should create a cupping with minimal fields."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()

        data = CuppingCreate(roast_id=roast.id)
        cupping = await repo.create(data)

        assert cupping.id is not None
        assert cupping.roast_id == roast.id
        assert cupping.aroma is None

    @pytest.mark.asyncio
    async def test_create_cupping_with_scores(self, mongo_db) -> None:
        """Should create a cupping with scores."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()

        data = CuppingCreate(
            roast_id=roast.id,
            rest_days=7,
            scores=CuppingScores(
                aroma=8.0,
                acidity=7.5,
                sweetness=8.5,
                body=7.0,
                aftertaste=7.5,
                overall=8.0,
            ),
            flavor_notes=["citrus", "floral", "honey"],
            defects=[],
            brew_method="V60",
            notes="Excellent clarity",
        )
        cupping = await repo.create(data)

        assert cupping.aroma == 8.0
        assert cupping.acidity == 7.5
        assert cupping.flavor_notes == ["citrus", "floral", "honey"]
        assert cupping.brew_method == "V60"

    @pytest.mark.asyncio
    async def test_create_cupping_with_defects(self, mongo_db) -> None:
        """Should create a cupping with defect notes."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()

        data = CuppingCreate(
            roast_id=roast.id,
            defects=["baked", "flat"],
        )
        cupping = await repo.create(data)

        assert cupping.defects == ["baked", "flat"]

    @pytest.mark.asyncio
    async def test_get_by_id(self, mongo_db) -> None:
        """Should retrieve a cupping by ID."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        created = await repo.create(CuppingCreate(roast_id=roast.id))

        result = await repo.get_by_id(created.id)

        assert result is not None
        assert result.id == created.id

    @pytest.mark.asyncio
    async def test_get_by_id_with_roast(self, mongo_db) -> None:
        """Should retrieve cupping with roast_id set."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        created = await repo.create(CuppingCreate(roast_id=roast.id))

        result = await repo.get_by_id_with_roast(created.id)

        assert result is not None
        assert result.roast_id == roast.id
        # In MongoDB, load roast separately
        roast_repo = RoastRepository()
        roast_obj = await roast_repo.get_by_id(result.roast_id)
        assert roast_obj is not None
        assert roast_obj.id == roast.id

    @pytest.mark.asyncio
    async def test_get_all(self, mongo_db) -> None:
        """Should retrieve all cuppings with pagination."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        for _ in range(5):
            await repo.create(CuppingCreate(roast_id=roast.id))

        result = await repo.get_all(limit=3)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_by_roast_id(self, mongo_db) -> None:
        """Should filter cuppings by roast ID."""
        roast1 = await self._create_roast(mongo_db)
        roast2 = await self._create_roast(mongo_db)
        repo = CuppingRepository()

        await repo.create(CuppingCreate(roast_id=roast1.id))
        await repo.create(CuppingCreate(roast_id=roast1.id))
        await repo.create(CuppingCreate(roast_id=roast2.id))

        result = await repo.get_by_roast_id(roast1.id)

        assert len(result) == 2
        assert all(c.roast_id == roast1.id for c in result)

    @pytest.mark.asyncio
    async def test_get_with_defects(self, mongo_db) -> None:
        """Should retrieve only cuppings with defects."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()

        # Cupping without defects
        await repo.create(CuppingCreate(roast_id=roast.id))
        # Cuppings with defects
        await repo.create(CuppingCreate(roast_id=roast.id, defects=["baked"]))
        await repo.create(CuppingCreate(roast_id=roast.id, defects=["grassy", "sour"]))

        result = await repo.get_with_defects()

        assert len(result) == 2
        for cupping in result:
            assert cupping.defects is not None
            assert len(cupping.defects) > 0

    @pytest.mark.asyncio
    async def test_update_cupping(self, mongo_db) -> None:
        """Should update a cupping record."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        created = await repo.create(CuppingCreate(roast_id=roast.id))

        updated = await repo.update(
            created.id,
            CuppingUpdate(
                rest_days=10,
                scores=CuppingScores(aroma=9.0, overall=8.5),
                notes="Updated notes",
            ),
        )

        assert updated is not None
        assert updated.rest_days == 10
        assert updated.aroma == 9.0
        assert updated.overall == 8.5
        assert updated.notes == "Updated notes"

    @pytest.mark.asyncio
    async def test_update_cupping_flavor_notes(self, mongo_db) -> None:
        """Should update flavor notes as native list."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        created = await repo.create(CuppingCreate(roast_id=roast.id))

        updated = await repo.update(
            created.id,
            CuppingUpdate(flavor_notes=["chocolate", "caramel"]),
        )

        assert updated is not None
        assert updated.flavor_notes == ["chocolate", "caramel"]

    @pytest.mark.asyncio
    async def test_delete_cupping(self, mongo_db) -> None:
        """Should delete a cupping record."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        created = await repo.create(CuppingCreate(roast_id=roast.id))

        result = await repo.delete(created.id)

        assert result is True
        assert await repo.get_by_id(created.id) is None

    @pytest.mark.asyncio
    async def test_count(self, mongo_db) -> None:
        """Should count total cuppings."""
        roast = await self._create_roast(mongo_db)
        repo = CuppingRepository()
        for _ in range(3):
            await repo.create(CuppingCreate(roast_id=roast.id))

        count = await repo.count()

        assert count == 3


# =============================================================================
# Cross-Repository Integration Tests
# =============================================================================


class TestCrossRepositoryIntegration:
    """Tests for interactions between repositories."""

    @pytest.mark.asyncio
    async def test_full_workflow(self, mongo_db) -> None:
        """Test a complete workflow: Coffee -> Profile -> Roast -> Cupping."""
        # Create coffee
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(
            CoffeeCreate(
                name="Ethiopia Yirgacheffe",
                origin="Ethiopia",
                processing="Washed",
            )
        )

        # Create profile for coffee
        profile_repo = ProfileRepository()
        profile = await profile_repo.create(
            ProfileCreate(
                name="Light Ethiopian",
                coffee_id=coffee.id,
                roast_level="Light",
                phases=_sample_phases(),
            )
        )

        # Create roast with profile
        roast_repo = RoastRepository()
        roast = await roast_repo.create(
            RoastCreate(
                coffee_id=coffee.id,
                profile_id=profile.id,
                batch_size_grams=500,
            )
        )

        # Update roast with events
        await roast_repo.update(
            roast.id,
            RoastUpdate(
                charge_temp=195.0,
                turning_point_time=75,
                turning_point_temp=97.0,
                fc_start_time=480,
                fc_start_temp=196.0,
                drop_time=540,
                drop_temp=205.0,
            ),
        )

        # Complete roast
        await roast_repo.complete(
            roast.id,
            end_time="2024-01-01T12:10:00",
            total_time_seconds=540,
            dtr=20.0,
            weight_loss=12.5,
        )

        # Add cupping notes
        cupping_repo = CuppingRepository()
        cupping = await cupping_repo.create(
            CuppingCreate(
                roast_id=roast.id,
                rest_days=7,
                scores=CuppingScores(
                    aroma=8.5,
                    acidity=8.0,
                    sweetness=8.5,
                    body=7.0,
                    aftertaste=8.0,
                    overall=8.0,
                ),
                flavor_notes=["citrus", "floral", "tea-like"],
                defects=[],
                brew_method="V60",
            )
        )

        # Verify full chain via separate queries (MongoDB has no ORM relationships)
        full_roast = await roast_repo.get_by_id(roast.id)
        assert full_roast is not None
        assert full_roast.coffee_id == coffee.id
        assert full_roast.profile_id == profile.id
        assert full_roast.status == RoastStatus.COMPLETED

        coffee_obj = await coffee_repo.get_by_id(full_roast.coffee_id)
        assert coffee_obj is not None
        assert coffee_obj.name == "Ethiopia Yirgacheffe"

        profile_obj = await profile_repo.get_by_id(full_roast.profile_id)
        assert profile_obj is not None
        assert profile_obj.name == "Light Ethiopian"

        cuppings = await cupping_repo.get_by_roast_id(roast.id)
        assert len(cuppings) == 1
        assert cuppings[0].id == cupping.id

    @pytest.mark.asyncio
    async def test_coffee_deletion_cascades(self, mongo_db) -> None:
        """Deleting coffee should cascade delete to profiles."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Test"))

        profile_repo = ProfileRepository()
        profile = await profile_repo.create(
            ProfileCreate(name="Test Profile", coffee_id=coffee.id, phases=_sample_phases())
        )

        # Coffee deletion cascades to profiles (manual in repo layer)
        await coffee_repo.delete(coffee.id)

        # Profile should be deleted due to cascade
        result = await profile_repo.get_by_id(profile.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_roast_deletion_cascades_to_cuppings(self, mongo_db) -> None:
        """Deleting roast should cascade to cuppings."""
        roast_repo = RoastRepository()
        roast = await roast_repo.create(RoastCreate())

        cupping_repo = CuppingRepository()
        cupping = await cupping_repo.create(CuppingCreate(roast_id=roast.id))
        cupping_id = cupping.id

        await roast_repo.delete(roast.id)

        # Cupping should be deleted due to cascade
        result = await cupping_repo.get_by_id(cupping_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_coffee_deletion_cascades_to_roasts_and_cuppings(self, mongo_db) -> None:
        """Deleting coffee should cascade to roasts and their cuppings."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Test"))

        roast_repo = RoastRepository()
        roast = await roast_repo.create(RoastCreate(coffee_id=coffee.id))

        cupping_repo = CuppingRepository()
        cupping = await cupping_repo.create(CuppingCreate(roast_id=roast.id))

        await coffee_repo.delete(coffee.id)

        # Both roast and cupping should be deleted
        assert await roast_repo.get_by_id(roast.id) is None
        assert await cupping_repo.get_by_id(cupping.id) is None

    @pytest.mark.asyncio
    async def test_profile_deletion_sets_null_on_roasts(self, mongo_db) -> None:
        """Deleting profile should SET NULL on roast.profile_id."""
        profile_repo = ProfileRepository()
        profile = await profile_repo.create(
            ProfileCreate(name="Test Profile", phases=_sample_phases())
        )

        roast_repo = RoastRepository()
        roast = await roast_repo.create(RoastCreate(profile_id=profile.id))

        await profile_repo.delete(profile.id)

        # Roast should still exist but profile_id should be None
        updated_roast = await roast_repo.get_by_id(roast.id)
        assert updated_roast is not None
        assert updated_roast.profile_id is None
