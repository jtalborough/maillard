"""Tests for database models and schemas."""

import pytest

from maillard.db.models import Coffee, Cupping, Profile, Roast, generate_uuid, utc_now
from maillard.db.repository import (
    CoffeeRepository,
    CuppingRepository,
    ProfileRepository,
    RoastRepository,
)
from maillard.db.schemas import (
    CoffeeCreate,
    CoffeeResponse,
    CurveDataSchema,
    CurveEventSchema,
    CuppingCreate,
    CuppingScores,
    PhaseSchema,
    ProfileCreate,
    ProfileResponse,
    RoastCreate,
)


class TestUUIDGeneration:
    """Tests for UUID generation."""

    def test_generate_uuid_returns_string(self) -> None:
        """generate_uuid should return a string."""
        result = generate_uuid()
        assert isinstance(result, str)

    def test_generate_uuid_is_valid_uuid_format(self) -> None:
        """generate_uuid should return a valid UUID format."""
        result = generate_uuid()
        # UUID format: 8-4-4-4-12 hex chars
        parts = result.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_generate_uuid_is_unique(self) -> None:
        """generate_uuid should return unique values."""
        uuids = [generate_uuid() for _ in range(100)]
        assert len(set(uuids)) == 100


class TestUtcNow:
    """Tests for timestamp generation."""

    def test_utc_now_returns_string(self) -> None:
        """utc_now should return a string."""
        result = utc_now()
        assert isinstance(result, str)

    def test_utc_now_is_iso_format(self) -> None:
        """utc_now should return ISO format timestamp."""
        result = utc_now()
        # Should contain date separator and time separator
        assert "T" in result
        # Should have at least YYYY-MM-DDTHH:MM:SS format
        assert len(result) >= 19


class TestCoffeeModel:
    """Tests for the Coffee model."""

    @pytest.mark.asyncio
    async def test_create_coffee_minimal(self, mongo_db) -> None:
        """Should create a coffee with only required fields."""
        coffee = Coffee(name="Test Coffee", origin="Ethiopia")
        await coffee.insert()

        assert coffee.id is not None
        assert coffee.name == "Test Coffee"
        assert coffee.origin == "Ethiopia"
        assert coffee.created_at is not None
        assert coffee.updated_at is not None

    @pytest.mark.asyncio
    async def test_create_coffee_full(self, mongo_db) -> None:
        """Should create a coffee with all fields."""
        coffee = Coffee(
            name="Yirgacheffe Natural",
            origin="Ethiopia",
            processing="Natural",
            varietal=["Heirloom"],
            altitude=1900,
            altitude_category="High",
            moisture=11.5,
            density="Medium",
            harvest_date="2024-01",
            purchase_date="2024-03-15",
            quantity_grams=5000,
            notes="Blueberry notes",
        )
        await coffee.insert()

        assert coffee.id is not None
        assert coffee.processing == "Natural"
        assert coffee.varietal == ["Heirloom"]
        assert coffee.altitude == 1900

    @pytest.mark.asyncio
    async def test_read_coffee(self, mongo_db) -> None:
        """Should read a coffee from the database."""
        coffee = Coffee(name="Test", origin="Kenya")
        await coffee.insert()
        coffee_id = coffee.id

        fetched = await Coffee.get(coffee_id)

        assert fetched is not None
        assert fetched.name == "Test"
        assert fetched.origin == "Kenya"


class TestProfileModel:
    """Tests for the Profile model."""

    @pytest.mark.asyncio
    async def test_create_profile_minimal(self, mongo_db) -> None:
        """Should create a profile with minimal fields."""
        phases = [
            {
                "name": "charge",
                "target_time_seconds": 0,
                "target_temp": 200,
                "heat_percent": 100,
                "fan_percent": 40,
                "drum_percent": 60,
            }
        ]
        profile = Profile(name="Test Profile", phases=phases)
        await profile.insert()

        assert profile.id is not None
        assert profile.name == "Test Profile"
        assert profile.version == 1

    @pytest.mark.asyncio
    async def test_profile_coffee_reference(self, mongo_db) -> None:
        """Should link profile to coffee via coffee_id."""
        coffee = Coffee(name="Test", origin="Colombia")
        await coffee.insert()

        phases = [{"name": "charge", "target_time_seconds": 0, "target_temp": 195, "heat_percent": 100, "fan_percent": 40, "drum_percent": 60}]
        profile = Profile(name="Colombia Light", coffee_id=coffee.id, phases=phases)
        await profile.insert()

        assert profile.coffee_id == coffee.id
        # In MongoDB, load coffee separately
        coffee_obj = await Coffee.get(profile.coffee_id)
        assert coffee_obj is not None
        assert coffee_obj.name == "Test"


class TestRoastModel:
    """Tests for the Roast model."""

    @pytest.mark.asyncio
    async def test_create_roast_minimal(self, mongo_db) -> None:
        """Should create a roast with minimal fields."""
        roast = Roast()
        await roast.insert()

        assert roast.id is not None
        assert roast.status == "in_progress"

    @pytest.mark.asyncio
    async def test_roast_with_events(self, mongo_db) -> None:
        """Should store roast events."""
        roast = Roast(
            charge_temp=195.5,
            turning_point_time=75,
            turning_point_temp=97.0,
            dry_end_time=240,
            dry_end_temp=155.0,
            fc_start_time=480,
            fc_start_temp=196.0,
            drop_time=600,
            drop_temp=210.0,
            dtr=25.0,
            status="completed",
        )
        await roast.insert()

        assert roast.charge_temp == 195.5
        assert roast.dtr == 25.0
        assert roast.status == "completed"

    @pytest.mark.asyncio
    async def test_roast_curve_data(self, mongo_db) -> None:
        """Should store curve data as native subdocument."""
        curve_data = {
            "time_seconds": [0, 1.5, 3.0],
            "bt": [195.0, 180.0, 165.0],
            "et": [220.0, 215.0, 210.0],
            "bt_ror": [None, -20.0, -15.0],
            "heat": [100, 100, 100],
            "fan": [40, 40, 45],
            "drum": [60, 60, 60],
            "events": [{"time": 0, "type": "charge", "temp": 195.0}],
        }
        roast = Roast(curve_data=curve_data)
        await roast.insert()

        # Verify native access
        assert roast.curve_data["bt"] == [195.0, 180.0, 165.0]
        assert roast.curve_data["events"][0]["type"] == "charge"

    @pytest.mark.asyncio
    async def test_roast_profile_reference(self, mongo_db) -> None:
        """Should link roast to profile via profile_id."""
        phases = [{"name": "charge", "target_time_seconds": 0, "target_temp": 195, "heat_percent": 100, "fan_percent": 40, "drum_percent": 60}]
        profile = Profile(name="Test Profile", phases=phases)
        await profile.insert()

        roast = Roast(profile_id=profile.id)
        await roast.insert()

        # In MongoDB, load profile separately
        profile_obj = await Profile.get(roast.profile_id)
        assert profile_obj is not None
        assert profile_obj.name == "Test Profile"


class TestCuppingModel:
    """Tests for the Cupping model."""

    @pytest.mark.asyncio
    async def test_create_cupping(self, mongo_db) -> None:
        """Should create a cupping record."""
        roast = Roast()
        await roast.insert()

        cupping = Cupping(
            roast_id=roast.id,
            rest_days=7,
            aroma=8.0,
            acidity=7.5,
            sweetness=8.5,
            body=7.0,
            aftertaste=7.5,
            overall=8.0,
            flavor_notes=["citrus", "floral", "honey"],
            defects=[],
            brew_method="V60",
        )
        await cupping.insert()

        assert cupping.id is not None
        assert cupping.aroma == 8.0
        assert cupping.flavor_notes == ["citrus", "floral", "honey"]

    @pytest.mark.asyncio
    async def test_cupping_roast_reference(self, mongo_db) -> None:
        """Should link cupping to roast via roast_id."""
        roast = Roast()
        await roast.insert()

        cupping = Cupping(roast_id=roast.id, overall=7.5)
        await cupping.insert()

        assert cupping.roast_id == roast.id
        # In MongoDB, load roast separately
        roast_obj = await Roast.get(cupping.roast_id)
        assert roast_obj is not None
        assert roast_obj.id == roast.id


class TestCascadeDelete:
    """Tests for cascade delete behavior (manual in repo layer)."""

    @pytest.mark.asyncio
    async def test_delete_coffee_cascades_to_profiles(self, mongo_db) -> None:
        """Deleting coffee should cascade to profiles."""
        coffee_repo = CoffeeRepository()
        coffee = await coffee_repo.create(CoffeeCreate(name="Test", origin="Brazil"))

        profile_repo = ProfileRepository()
        profile = await profile_repo.create(
            ProfileCreate(
                name="Test Profile",
                coffee_id=coffee.id,
                phases=[PhaseSchema(name="charge", target_time_seconds=0, target_temp=195, heat_percent=100, fan_percent=40, drum_percent=60)],
            )
        )
        profile_id = profile.id

        await coffee_repo.delete(coffee.id)

        assert await profile_repo.get_by_id(profile_id) is None

    @pytest.mark.asyncio
    async def test_delete_roast_cascades_to_cuppings(self, mongo_db) -> None:
        """Deleting roast should cascade to cuppings."""
        roast_repo = RoastRepository()
        roast = await roast_repo.create(RoastCreate())

        cupping_repo = CuppingRepository()
        cupping = await cupping_repo.create(CuppingCreate(roast_id=roast.id))
        cupping_id = cupping.id

        await roast_repo.delete(roast.id)

        assert await cupping_repo.get_by_id(cupping_id) is None


class TestPhaseSchema:
    """Tests for PhaseSchema validation."""

    def test_valid_phase(self) -> None:
        """Should accept valid phase data."""
        phase = PhaseSchema(
            name="charge",
            target_time_seconds=0,
            target_temp=195.0,
            heat_percent=100,
            fan_percent=40,
            drum_percent=60,
            target_ror=None,
            notes="Start with high heat",
        )
        assert phase.name == "charge"
        assert phase.heat_percent == 100

    def test_phase_percent_validation(self) -> None:
        """Should reject invalid percentages."""
        with pytest.raises(ValueError):
            PhaseSchema(
                name="test",
                target_time_seconds=0,
                target_temp=195.0,
                heat_percent=150,  # Invalid: > 100
                fan_percent=40,
                drum_percent=60,
            )


class TestCurveDataSchema:
    """Tests for CurveDataSchema validation."""

    def test_valid_curve_data(self) -> None:
        """Should accept valid curve data."""
        curve = CurveDataSchema(
            time_seconds=[0, 1.5, 3.0],
            bt=[195.0, 180.0, 165.0],
            et=[220.0, 215.0, 210.0],
            bt_ror=[None, -20.0, -15.0],
            heat=[100, 100, 100],
            fan=[40, 40, 45],
            drum=[60, 60, 60],
            events=[CurveEventSchema(time=0, type="charge", temp=195.0)],
        )
        assert len(curve.bt) == 3
        assert curve.events[0].type == "charge"

    def test_empty_curve_data(self) -> None:
        """Should accept empty curve data."""
        curve = CurveDataSchema()
        assert curve.time_seconds == []
        assert curve.events == []


class TestCoffeeSchemas:
    """Tests for Coffee schemas."""

    def test_coffee_create(self) -> None:
        """Should validate CoffeeCreate."""
        coffee = CoffeeCreate(
            name="Test Coffee",
            origin="Ethiopia",
            processing="Washed",
            varietal=["SL28", "SL34"],
            altitude=1800,
        )
        assert coffee.name == "Test Coffee"
        assert coffee.varietal == ["SL28", "SL34"]

    def test_coffee_response_from_dict(self) -> None:
        """Should create CoffeeResponse from dict."""
        model_data = {
            "id": "test-uuid",
            "name": "Test",
            "origin": "Kenya",
            "processing": None,
            "varietal": None,
            "altitude": None,
            "altitude_category": None,
            "moisture": None,
            "density": None,
            "harvest_date": None,
            "purchase_date": None,
            "quantity_grams": None,
            "notes": None,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }
        response = CoffeeResponse.model_validate(model_data)
        assert response.id == "test-uuid"
        assert response.name == "Test"


class TestProfileSchemas:
    """Tests for Profile schemas."""

    def test_profile_create(self) -> None:
        """Should validate ProfileCreate."""
        profile = ProfileCreate(
            name="Light Roast",
            roast_level="Light",
            flavor_goal="Highlight floral notes",
            phases=[
                PhaseSchema(
                    name="charge",
                    target_time_seconds=0,
                    target_temp=195.0,
                    heat_percent=100,
                    fan_percent=40,
                    drum_percent=60,
                ),
                PhaseSchema(
                    name="drop",
                    target_time_seconds=540,
                    target_temp=205.0,
                    heat_percent=60,
                    fan_percent=60,
                    drum_percent=60,
                ),
            ],
            target_dtr=20.0,
        )
        assert profile.name == "Light Roast"
        assert len(profile.phases) == 2
        assert profile.target_dtr == 20.0

    def test_profile_requires_phases(self) -> None:
        """Should require at least one phase."""
        with pytest.raises(ValueError):
            ProfileCreate(name="No Phases", phases=[])


class TestCuppingSchemas:
    """Tests for Cupping schemas."""

    def test_cupping_create(self) -> None:
        """Should validate CuppingCreate."""
        cupping = CuppingCreate(
            roast_id="test-roast-id",
            rest_days=7,
            scores=CuppingScores(
                aroma=8.0,
                acidity=7.5,
                sweetness=8.5,
                body=7.0,
                aftertaste=7.5,
                overall=8.0,
            ),
            flavor_notes=["citrus", "floral"],
            defects=[],
            brew_method="V60",
        )
        assert cupping.scores.aroma == 8.0
        assert cupping.flavor_notes == ["citrus", "floral"]

    def test_cupping_score_validation(self) -> None:
        """Should reject scores outside 1-10 range."""
        with pytest.raises(ValueError):
            CuppingScores(aroma=11.0)  # Invalid: > 10

        with pytest.raises(ValueError):
            CuppingScores(acidity=0.5)  # Invalid: < 1


class TestModelToSchemaConversion:
    """Tests for model to Pydantic schema conversion."""

    @pytest.mark.asyncio
    async def test_coffee_to_response(self, mongo_db) -> None:
        """Should convert Coffee model with native varietal to CoffeeResponse."""
        from maillard.db.converters import coffee_to_response

        coffee = Coffee(
            name="Test Coffee",
            origin="Ethiopia",
            varietal=["Heirloom", "74110"],
        )
        await coffee.insert()

        response = coffee_to_response(coffee)

        assert response.id == coffee.id
        assert response.name == "Test Coffee"
        assert response.varietal == ["Heirloom", "74110"]
        assert isinstance(response.varietal, list)

    @pytest.mark.asyncio
    async def test_profile_to_response(self, mongo_db) -> None:
        """Should convert Profile model with native phases to ProfileResponse."""
        from maillard.db.converters import profile_to_response

        phases_data = [
            {
                "name": "charge",
                "target_time_seconds": 0,
                "target_temp": 195.0,
                "heat_percent": 100,
                "fan_percent": 40,
                "drum_percent": 60,
            },
            {
                "name": "drop",
                "target_time_seconds": 540,
                "target_temp": 210.0,
                "heat_percent": 50,
                "fan_percent": 70,
                "drum_percent": 60,
            },
        ]
        profile = Profile(
            name="Light Roast",
            phases=phases_data,
            roast_level="Light",
        )
        await profile.insert()

        response = profile_to_response(profile)

        assert response.id == profile.id
        assert response.name == "Light Roast"
        assert len(response.phases) == 2
        assert response.phases[0].name == "charge"
        assert response.phases[0].target_temp == 195.0
        assert response.phases[1].name == "drop"

    @pytest.mark.asyncio
    async def test_roast_to_response_with_curve(self, mongo_db) -> None:
        """Should convert Roast model with native curve_data to RoastWithCurve."""
        from maillard.db.converters import roast_to_response_with_curve

        curve_data = {
            "time_seconds": [0, 1.5, 3.0],
            "bt": [195.0, 180.0, 165.0],
            "et": [220.0, 215.0, 210.0],
            "bt_ror": [None, -20.0, -15.0],
            "heat": [100, 100, 100],
            "fan": [40, 40, 45],
            "drum": [60, 60, 60],
            "events": [{"time": 0, "type": "charge", "temp": 195.0}],
        }
        roast = Roast(
            curve_data=curve_data,
            status="completed",
        )
        await roast.insert()

        response = roast_to_response_with_curve(roast)

        assert response.id == roast.id
        assert response.curve_data is not None
        assert response.curve_data.bt == [195.0, 180.0, 165.0]
        assert len(response.curve_data.events) == 1
        assert response.curve_data.events[0].type == "charge"

    @pytest.mark.asyncio
    async def test_cupping_to_response(self, mongo_db) -> None:
        """Should convert Cupping model with native lists to CuppingResponse."""
        from maillard.db.converters import cupping_to_response

        roast = Roast()
        await roast.insert()

        cupping = Cupping(
            roast_id=roast.id,
            aroma=8.5,
            acidity=7.0,
            flavor_notes=["citrus", "floral", "honey"],
            defects=["slight bitterness"],
        )
        await cupping.insert()

        response = cupping_to_response(cupping)

        assert response.id == cupping.id
        assert response.aroma == 8.5
        assert response.flavor_notes == ["citrus", "floral", "honey"]
        assert response.defects == ["slight bitterness"]
        assert isinstance(response.flavor_notes, list)
