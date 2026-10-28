"""Beanie ODM document models for Maillard.

Defines the MongoDB document models for coffees, profiles, roasts, and cuppings.
Uses native types for nested data (lists, subdocuments) instead of JSON strings.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from beanie import Document
from pydantic import Field, computed_field, model_validator
from pymongo import IndexModel


class RoastStatus(StrEnum):
    """Roast session status values."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"
    ABANDONED = "abandoned"


def generate_uuid() -> str:
    """Generate a new UUID string."""
    return str(uuid4())


def utc_now() -> str:
    """Get current UTC time as ISO format string."""
    return datetime.now(UTC).isoformat()


class Coffee(Document):
    """Green coffee inventory document.

    Stores information about green coffee beans including origin,
    processing method, and physical characteristics.
    """

    id: str = Field(default_factory=generate_uuid, alias="_id")
    name: str
    origin: str
    altitude_m: int | None = Field(default=None, ge=0, le=3000)
    moisture_pct: float | None = Field(default=None, ge=6, le=16, allow_inf_nan=False)
    density_g_l: float | None = Field(default=None, ge=500, le=900, allow_inf_nan=False)
    processing: str | None = None  # Washed, Natural, Honey, Anaerobic
    varietal: list[str] | None = None  # Native list
    altitude: int | None = None  # MASL
    altitude_category: str | None = None  # Low/Medium/High
    moisture: float | None = None  # Percentage
    density: str | None = None  # Low/Medium/High
    harvest_date: str | None = None  # ISO date
    purchase_date: str | None = None  # ISO date
    quantity_grams: int | None = None
    notes: str | None = None
    published: bool = False
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "coffees"
        indexes = [
            IndexModel([("origin", 1)]),
            IndexModel([("created_at", -1)]),
        ]


class Profile(Document):
    """Roast profile document.

    Stores roast profiles including target parameters for each phase.
    Phases are stored as native subdocuments (list of dicts).
    """

    id: str = Field(default_factory=generate_uuid, alias="_id")
    coffee_id: str | None = None
    name: str
    roast_level: str | None = None  # Light, Medium-Light, Medium, Medium-Dark, Dark
    flavor_goal: str | None = None
    brew_method: str | None = None  # Filter, Espresso, Both
    batch_size_grams: int | None = None
    phases: list[dict] = Field(default_factory=list)  # Native subdocument array
    target_dtr: float | None = None  # Development Time Ratio %
    target_weight_loss: float | None = None  # %
    charge_temp: float | None = None  # °C
    ai_rationale: str | None = None
    plan_source: str | None = None  # heuristic | ai | manual (how phases were produced)
    ai_model: str | None = None  # model id when plan_source == "ai"
    warnings: list[str] = Field(default_factory=list)
    version: int = 1
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "profiles"
        indexes = [
            IndexModel([("coffee_id", 1)]),
            IndexModel([("roast_level", 1)]),
        ]


class Roast(Document):
    """Roast session document.

    Stores data for individual roasting sessions including timing,
    key events, calculated metrics, and curve data.
    """

    id: str = Field(default_factory=generate_uuid, alias="_id")
    profile_id: str | None = None
    plan_id: str | None = None  # RoastPlan locked at charge
    coffee_id: str | None = None
    batch_size_grams: int | None = None

    # Timing
    start_time: str | None = None  # ISO timestamp
    end_time: str | None = None
    total_time_seconds: int | None = None

    # Key events (time in seconds from charge)
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

    # Calculated metrics
    dtr: float | None = None  # Development Time Ratio %
    weight_loss: float | None = None  # %

    # Curve data (native subdocument)
    curve_data: dict | None = None
    connection_events: list[dict] | None = None

    # Environment
    ambient_temp: float | None = None
    ambient_humidity: float | None = None

    # Status
    excluded: bool = False
    status: str = RoastStatus.IN_PROGRESS
    notes: str | None = None
    published: bool = False
    control_mode: str = "manual"  # manual | assist (AI suggest) | auto (AI control)

    # Label settings
    bond_number: int | None = None  # Drop number (displayed as "DROP #001")
    weight_display: str | None = None
    cupping_score: float | None = None  # Quality score
    flavor_notes: list[str] | None = None  # Tasting notes
    roast_name: str | None = None  # Auto-generated memorable name
    code: str | None = None  # 6-char Crockford base32 public code (QR / site URL)

    site_url: str | None = None
    site_synced_at: str | None = None
    site_synced_code: str | None = None
    site_sync_status: str | None = None
    site_published: bool | None = None  # effective published flag returned by the site
    site_warnings: list[str] = Field(default_factory=list)

    # Weights (grams). weight_loss (%) is computed from these when both are set.
    green_weight_grams: float | None = None
    roasted_weight_grams: float | None = None
    bags_packed: int | None = Field(default=None, ge=0)
    bag_weights_grams: list[float] = Field(default_factory=list)
    bag_size_grams: float = Field(default=340, gt=0, allow_inf_nan=False)
    drop_id: str | None = None
    green_deducted: bool = False

    created_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "roasts"
        indexes = [
            IndexModel([("code", 1)], unique=True, sparse=True),
            IndexModel([("coffee_id", 1)]),
            IndexModel([("profile_id", 1)]),
            IndexModel([("status", 1)]),
            IndexModel([("start_time", -1)]),
        ]


class Cupping(Document):
    """Cupping notes document.

    Stores cupping evaluation data for a roast including SCA scores
    and flavor descriptors.
    """

    id: str = Field(default_factory=generate_uuid, alias="_id")
    supabase_id: str | None = None  # Supabase record ID for dedup on pull
    roast_id: str
    rest_days: int | None = None

    # SCA cupping scores (1-10 scale)
    aroma: float | None = None
    acidity: float | None = None
    sweetness: float | None = None
    body: float | None = None
    aftertaste: float | None = None
    overall: float | None = None

    # Descriptors (native lists)
    flavor_notes: list[str] | None = None
    defects: list[str] | None = None

    brew_method: str | None = None
    cupper_name: str | None = None
    next_time_notes: str | None = None
    notes: str | None = None
    cupped_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "cuppings"
        indexes = [
            IndexModel([("roast_id", 1)]),
        ]


class RoastPlan(Document):
    """Planned roast, frozen at charge and linked to exactly one roast.

    A snapshot of the selected (often AI-generated) profile plus the batch
    actually charged, derived targets and the planned BT curve. Profiles can
    be edited later; the plan a roast was made against never changes.
    """

    id: str = Field(default_factory=generate_uuid, alias="_id")
    roast_id: str | None = None
    profile_id: str | None = None
    profile_version: int | None = None
    coffee_id: str | None = None
    name: str
    source: str = "manual"  # heuristic | ai | manual
    ai_model: str | None = None
    rationale: str | None = None
    roast_level: str | None = None
    brew_method: str | None = None
    flavor_goal: str | None = None
    batch_size_grams: int | None = None
    charge_temp: float | None = None
    target_dtr: float | None = None
    target_fc_seconds: int | None = None
    target_drop_temp: float | None = None
    target_total_seconds: int | None = None
    phases: list[dict] = Field(default_factory=list)
    planned_curve: dict = Field(default_factory=dict)  # {"time_seconds": [...], "bt": [...]}
    warnings: list[str] = Field(default_factory=list)
    locked_at: str | None = None
    created_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "roast_plans"
        indexes = [
            IndexModel([("roast_id", 1)]),
            IndexModel([("profile_id", 1)]),
        ]


class SyncOutbox(Document):
    """Durable finished-roast ingest queue."""

    roast_id: str
    code: str | None = None
    status: Literal["pending", "synced", "failed"] = "pending"
    attempts: int = 0
    next_attempt_at: float = 0
    last_status_code: int | None = None
    last_error: str | None = None
    url: str | None = None
    synced_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "sync_outbox"
        indexes = [IndexModel([("roast_id", 1)], unique=True)]


class GreenLot(Document):
    """Receipt metadata and recoverable ledger balance cache."""

    id: str = Field(default_factory=generate_uuid, alias="_id")
    coffee_id: str
    received_at: str = Field(default_factory=utc_now)
    supplier: str | None = None
    cost_per_kg: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    grams_in: float = Field(ge=0, allow_inf_nan=False)
    grams_remaining: float = 0
    notes: str | None = None
    created_at: str = Field(default_factory=utc_now)
    applied_moves: list[str] = Field(default_factory=list, exclude=True)

    class Settings:
        name = "green_lots"
        indexes = [IndexModel([("coffee_id", 1), ("received_at", 1), ("_id", 1)])]


class InventoryMove(Document):
    """Immutable ledger entry; use the inventory service for all writes."""

    id: str = Field(default_factory=generate_uuid, alias="_id")
    at: str = Field(default_factory=utc_now)
    lot_id: str
    coffee_id: str
    kind: Literal["received", "roasted", "adjusted", "reserved", "released"]
    grams: float = Field(allow_inf_nan=False)
    roast_id: str | None = None
    drop_id: str | None = None
    idempotency_key: str | None = None
    note: str | None = None
    actor: str = "operator"

    @model_validator(mode="after")
    def positive_magnitude(self):
        if self.kind != "adjusted" and self.grams <= 0:
            raise ValueError("Movement magnitude must be positive")
        return self

    class Settings:
        name = "inventory_moves"
        keep_nulls = False
        indexes = [
            IndexModel([("idempotency_key", 1)], unique=True, sparse=True),
            IndexModel([("coffee_id", 1), ("at", 1)]),
            IndexModel([("drop_id", 1), ("lot_id", 1)]),
        ]


class PresaleDrop(Document):
    id: str = Field(default_factory=generate_uuid, alias="_id")
    coffee_id: str
    drop_number: int | None = None
    status: Literal["open", "closed", "roasted", "cancelled"] = "open"
    capacity_bags: int = Field(ge=0)
    bag_size_grams: float = Field(default=340, gt=0, allow_inf_nan=False)
    expected_yield: float = Field(default=0.85, gt=0, le=1, allow_inf_nan=False)
    bags_sold: int = Field(default=0, ge=0)
    bags_fulfilled: int = Field(default=0, ge=0)
    bags_held: int = Field(default=0, ge=0)
    site_sync_attempted: bool = Field(default=False, exclude=True)
    roast_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    closed_at: str | None = None

    @computed_field
    @property
    def green_grams_per_bag(self) -> float:
        return self.bag_size_grams / self.expected_yield

    class Settings:
        name = "presale_drops"
        indexes = [IndexModel([("coffee_id", 1)]), IndexModel([("status", 1)])]


class InventoryOperation(Document):
    """Internal durable write plan; recovery never edits ledger entries."""

    id: str = Field(alias="_id")
    coffee_id: str
    plan: dict
    done: bool = False
    created_at: str = Field(default_factory=utc_now)

    class Settings:
        name = "inventory_operations"
        indexes = [IndexModel([("coffee_id", 1), ("done", 1)])]


# Every Beanie document model; used by init_db() and the test fixtures.
DOCUMENT_MODELS: list[type[Document]] = [
    Coffee,
    Profile,
    Roast,
    Cupping,
    RoastPlan,
    SyncOutbox,
    GreenLot,
    InventoryMove,
    PresaleDrop,
    InventoryOperation,
]


__all__ = [
    "DOCUMENT_MODELS",
    "Coffee",
    "GreenLot",
    "InventoryMove",
    "PresaleDrop",
    "InventoryOperation",
    "Cupping",
    "Profile",
    "Roast",
    "RoastPlan",
    "RoastStatus",
    "SyncOutbox",
    "generate_uuid",
    "utc_now",
]
