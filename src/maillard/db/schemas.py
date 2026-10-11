"""Pydantic schemas for Maillard request/response validation.

Defines schemas for API/MCP request validation and response serialization.
"""

from pydantic import BaseModel, Field

# =============================================================================
# Phase Schema (used in Profile.phases JSON)
# =============================================================================


class PhaseSchema(BaseModel):
    """A single phase in a roast profile."""

    name: str = Field(
        ...,
        description="Phase name: charge, turning_point, dry_end, maillard, fc_start, development, drop",
    )
    target_time_seconds: int = Field(..., ge=0, description="Seconds from charge")
    target_temp: float = Field(..., description="Target temperature in Celsius")
    heat_percent: int = Field(..., ge=0, le=100, description="Heating power 0-100%")
    fan_percent: int = Field(..., ge=0, le=100, description="Fan speed 0-100%")
    drum_percent: int = Field(..., ge=0, le=100, description="Drum speed 0-100%")
    target_ror: float | None = Field(default=None, description="Target RoR in °C/min")
    notes: str | None = Field(default=None, description="Optional notes for this phase")


# =============================================================================
# Curve Data Schema (used in Roast.curve_data JSON)
# =============================================================================


class CurveEventSchema(BaseModel):
    """A roast event marker in the curve data."""

    time: int = Field(..., ge=0, description="Time in seconds from charge")
    type: str = Field(
        ...,
        description="Event type: charge, turning_point, dry_end, fc_start, fc_end, sc_start, drop",
    )
    temp: float = Field(..., description="Temperature at event in Celsius")


class CurveDataSchema(BaseModel):
    """Complete curve data for a roast."""

    gaps: list[dict] = Field(default_factory=list)
    connection_summary: dict = Field(default_factory=dict)
    time_seconds: list[float] = Field(default_factory=list, description="Sample times in seconds")
    bt: list[float] = Field(default_factory=list, description="Bean temperatures in Celsius")
    et: list[float] = Field(default_factory=list, description="Environment temperatures in Celsius")
    bt_ror: list[float | None] = Field(
        default_factory=list, description="BT Rate of Rise in °C/min"
    )
    heat: list[int] = Field(default_factory=list, description="Heat setting percentages")
    fan: list[int] = Field(default_factory=list, description="Fan setting percentages")
    drum: list[int] = Field(default_factory=list, description="Drum setting percentages")
    events: list[CurveEventSchema] = Field(default_factory=list, description="Roast event markers")


# =============================================================================
# Coffee Schemas
# =============================================================================


class CoffeeBase(BaseModel):
    """Base coffee schema with common fields."""

    name: str = Field(..., min_length=1, max_length=255, description="Coffee name")
    origin: str = Field(..., min_length=1, max_length=255, description="Country/region of origin")
    altitude_m: int | None = Field(default=None, ge=0, le=3000)
    moisture_pct: float | None = Field(default=None, ge=6, le=16, allow_inf_nan=False)
    density_g_l: float | None = Field(default=None, ge=500, le=900, allow_inf_nan=False)
    processing: str | None = Field(
        default=None, description="Processing method: Washed, Natural, Honey, Anaerobic"
    )
    varietal: list[str] | None = Field(
        default=None, description="Varietal names, e.g., ['Heirloom', 'SL28']"
    )
    altitude: int | None = Field(default=None, ge=0, description="Altitude in MASL")
    altitude_category: str | None = Field(
        default=None, description="Altitude category: Low, Medium, High"
    )
    moisture: float | None = Field(default=None, ge=0, le=100, description="Moisture percentage")
    density: str | None = Field(default=None, description="Density category: Low, Medium, High")
    harvest_date: str | None = Field(default=None, description="Harvest date (ISO format)")
    purchase_date: str | None = Field(default=None, description="Purchase date (ISO format)")
    quantity_grams: int | None = Field(default=None, ge=0, description="Quantity in grams")
    notes: str | None = Field(default=None, description="Additional notes")


class CoffeeCreate(CoffeeBase):
    """Schema for creating a new coffee."""


class CoffeeUpdate(BaseModel):
    """Schema for updating a coffee. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    origin: str | None = Field(default=None, min_length=1, max_length=255)
    altitude_m: int | None = Field(default=None, ge=0, le=3000)
    moisture_pct: float | None = Field(default=None, ge=6, le=16, allow_inf_nan=False)
    density_g_l: float | None = Field(default=None, ge=500, le=900, allow_inf_nan=False)
    processing: str | None = None
    varietal: list[str] | None = None
    altitude: int | None = None
    altitude_category: str | None = None
    moisture: float | None = None
    density: str | None = None
    harvest_date: str | None = None
    purchase_date: str | None = None
    quantity_grams: int | None = None
    notes: str | None = None


class CoffeeResponse(CoffeeBase):
    """Schema for coffee response."""

    id: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class CoffeeSummary(BaseModel):
    """Brief coffee summary for list responses."""

    id: str
    name: str
    origin: str
    processing: str | None = None

    model_config = {"from_attributes": True}


# =============================================================================
# Profile Schemas
# =============================================================================


class ProfileBase(BaseModel):
    """Base profile schema with common fields."""

    name: str = Field(..., min_length=1, max_length=255, description="Profile name")
    coffee_id: str | None = Field(default=None, description="Associated coffee ID")
    roast_level: str | None = Field(
        default=None, description="Roast level: Light, Medium-Light, Medium, Medium-Dark, Dark"
    )
    flavor_goal: str | None = Field(default=None, description="Desired flavor characteristics")
    brew_method: str | None = Field(
        default=None, description="Target brew method: Filter, Espresso, Both"
    )
    batch_size_grams: int | None = Field(default=None, ge=0, description="Batch size in grams")
    phases: list[PhaseSchema] = Field(..., min_length=1, description="Roast phase definitions")
    target_dtr: float | None = Field(
        default=None, ge=0, le=100, description="Target Development Time Ratio %"
    )
    target_weight_loss: float | None = Field(
        default=None, ge=0, le=50, description="Target weight loss %"
    )
    charge_temp: float | None = Field(default=None, description="Charge temperature in Celsius")
    ai_rationale: str | None = Field(
        default=None, description="AI explanation for profile choices"
    )
    plan_source: str | None = Field(
        default=None, description="How phases were produced: heuristic, ai, manual"
    )
    ai_model: str | None = Field(default=None, description="Model id for AI plans")
    warnings: list[str] = Field(default_factory=list)


class ProfileCreate(ProfileBase):
    """Schema for creating a new profile."""


class ProfileUpdate(BaseModel):
    """Schema for updating a profile. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    coffee_id: str | None = None
    roast_level: str | None = None
    flavor_goal: str | None = None
    brew_method: str | None = None
    batch_size_grams: int | None = None
    phases: list[PhaseSchema] | None = None
    target_dtr: float | None = None
    target_weight_loss: float | None = None
    charge_temp: float | None = None
    ai_rationale: str | None = None
    plan_source: str | None = None
    ai_model: str | None = None
    warnings: list[str] | None = None


class ProfileResponse(BaseModel):
    """Schema for profile response."""

    id: str
    name: str
    coffee_id: str | None
    roast_level: str | None
    flavor_goal: str | None
    brew_method: str | None
    batch_size_grams: int | None
    phases: list[PhaseSchema]
    target_dtr: float | None
    target_weight_loss: float | None
    charge_temp: float | None
    ai_rationale: str | None
    plan_source: str | None = None
    ai_model: str | None = None
    warnings: list[str] = Field(default_factory=list)
    version: int
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class ProfileSummary(BaseModel):
    """Brief profile summary for list responses."""

    id: str
    name: str
    coffee_id: str | None = None
    roast_level: str | None = None
    brew_method: str | None = None

    model_config = {"from_attributes": True}


# =============================================================================
# Roast Schemas
# =============================================================================


class RoastCreate(BaseModel):
    """Schema for creating a new roast."""

    profile_id: str | None = Field(default=None, description="Profile ID to use")
    coffee_id: str | None = Field(default=None, description="Coffee ID being roasted")
    batch_size_grams: int | None = Field(default=None, ge=0, description="Batch size in grams")
    ambient_temp: float | None = Field(default=None, description="Ambient temperature in Celsius")
    ambient_humidity: float | None = Field(
        default=None, ge=0, le=100, description="Ambient humidity %"
    )


class RoastUpdate(BaseModel):
    """Schema for updating a roast."""

    excluded: bool | None = None
    batch_size_grams: int | None = None
    charge_temp: float | None = None
    turning_point_time: int | None = None
    turning_point_temp: float | None = None
    dry_end_time: int | None = None
    dry_end_temp: float | None = None
    fc_start_time: int | None = None
    fc_start_temp: float | None = None
    fc_end_time: int | None = None
    fc_end_temp: float | None = None
    sc_start_time: int | None = None
    sc_start_temp: float | None = None
    drop_time: int | None = None
    drop_temp: float | None = None
    dtr: float | None = None
    weight_loss: float | None = None
    status: str | None = None
    notes: str | None = None
    bond_number: int | None = None
    weight_display: str | None = None
    cupping_score: float | None = None
    flavor_notes: list[str] | None = None
    roast_name: str | None = None
    green_weight_grams: float | None = Field(default=None, ge=0, le=5000)
    roasted_weight_grams: float | None = Field(default=None, ge=0, le=5000)
    published: bool | None = None


class RoastResponse(BaseModel):
    """Schema for roast response."""

    excluded: bool = False
    id: str
    profile_id: str | None
    coffee_id: str | None
    batch_size_grams: int | None
    start_time: str | None
    end_time: str | None
    total_time_seconds: int | None
    charge_temp: float | None
    turning_point_time: int | None
    turning_point_temp: float | None
    dry_end_time: int | None
    dry_end_temp: float | None
    fc_start_time: int | None
    fc_start_temp: float | None
    fc_end_time: int | None
    fc_end_temp: float | None
    sc_start_time: int | None
    sc_start_temp: float | None
    drop_time: int | None
    drop_temp: float | None
    dtr: float | None
    weight_loss: float | None
    ambient_temp: float | None
    ambient_humidity: float | None
    status: str
    notes: str | None
    bond_number: int | None = None
    weight_display: str | None = None
    cupping_score: float | None = None
    flavor_notes: list[str] | None = None
    roast_name: str | None = None
    code: str | None = None
    green_weight_grams: float | None = None
    roasted_weight_grams: float | None = None
    bags_packed: int | None = None
    bag_size_grams: float = 340
    drop_id: str | None = None
    green_deducted: bool = False
    plan_id: str | None = None
    control_mode: str | None = None
    published: bool = False
    created_at: str

    model_config = {"from_attributes": True}


class RoastWithCurve(RoastResponse):
    """Roast response including curve data."""

    curve_data: CurveDataSchema | None = None
    connection_events: list[dict] | None = None


class RoastSummary(BaseModel):
    """Brief roast summary for list responses."""

    excluded: bool = False
    id: str
    coffee_id: str | None
    profile_id: str | None
    start_time: str | None
    status: str
    drop_temp: float | None = None
    dtr: float | None = None

    model_config = {"from_attributes": True}


# =============================================================================
# Cupping Schemas
# =============================================================================


class CuppingScores(BaseModel):
    """SCA cupping scores (1-10 scale)."""

    aroma: float | None = Field(default=None, ge=1, le=10)
    acidity: float | None = Field(default=None, ge=1, le=10)
    sweetness: float | None = Field(default=None, ge=1, le=10)
    body: float | None = Field(default=None, ge=1, le=10)
    aftertaste: float | None = Field(default=None, ge=1, le=10)
    overall: float | None = Field(default=None, ge=1, le=10)


class CuppingCreate(BaseModel):
    """Schema for creating a cupping record."""

    roast_id: str = Field(..., description="ID of the roast being cupped")
    rest_days: int | None = Field(default=None, ge=0, description="Days since roast")
    scores: CuppingScores | None = Field(default=None, description="SCA scores")
    flavor_notes: list[str] | None = Field(
        default=None, description="Flavor descriptors: ['citrus', 'floral']"
    )
    defects: list[str] | None = Field(
        default=None, description="Defect notes: ['baked', 'grassy']"
    )
    brew_method: str | None = Field(default=None, description="How the coffee was brewed")
    cupper_name: str | None = Field(default=None, max_length=100, description="Name of the cupper")
    next_time_notes: str | None = None
    notes: str | None = Field(default=None, description="Additional notes")


class CuppingUpdate(BaseModel):
    """Schema for updating a cupping record."""

    rest_days: int | None = None
    scores: CuppingScores | None = None
    flavor_notes: list[str] | None = None
    defects: list[str] | None = None
    brew_method: str | None = None
    cupper_name: str | None = None
    next_time_notes: str | None = None
    notes: str | None = None


class CuppingResponse(BaseModel):
    """Schema for cupping response."""

    id: str
    roast_id: str
    rest_days: int | None
    aroma: float | None
    acidity: float | None
    sweetness: float | None
    body: float | None
    aftertaste: float | None
    overall: float | None
    flavor_notes: list[str] | None
    defects: list[str] | None
    brew_method: str | None
    cupper_name: str | None
    next_time_notes: str | None = None
    notes: str | None
    cupped_at: str

    model_config = {"from_attributes": True}


__all__ = [
    # Phase/Curve
    "CurveDataSchema",
    "CurveEventSchema",
    "PhaseSchema",
    # Coffee
    "CoffeeBase",
    "CoffeeCreate",
    "CoffeeResponse",
    "CoffeeSummary",
    "CoffeeUpdate",
    # Profile
    "ProfileBase",
    "ProfileCreate",
    "ProfileResponse",
    "ProfileSummary",
    "ProfileUpdate",
    # Roast
    "RoastCreate",
    "RoastResponse",
    "RoastSummary",
    "RoastUpdate",
    "RoastWithCurve",
    # Cupping
    "CuppingCreate",
    "CuppingResponse",
    "CuppingScores",
    "CuppingUpdate",
]
