"""Model-to-schema conversion helpers for Maillard.

Provides functions to convert between Beanie document models and
Pydantic response schemas used in the API layer.

With MongoDB, nested data (lists, subdocuments) is stored natively,
so no JSON string parsing is needed.
"""

from maillard.db.models import Coffee, Cupping, Profile, Roast
from maillard.db.schemas import (
    CoffeeResponse,
    CuppingResponse,
    CurveDataSchema,
    PhaseSchema,
    ProfileResponse,
    RoastResponse,
    RoastWithCurve,
)


def coffee_to_response(coffee: Coffee) -> CoffeeResponse:
    """Convert a Coffee document to CoffeeResponse schema."""
    return CoffeeResponse(
        id=coffee.id,
        name=coffee.name,
        origin=coffee.origin,
        processing=coffee.processing,
        varietal=coffee.varietal,
        altitude_m=coffee.altitude_m,
        moisture_pct=coffee.moisture_pct,
        density_g_l=coffee.density_g_l,
        altitude=coffee.altitude,
        altitude_category=coffee.altitude_category,
        moisture=coffee.moisture,
        density=coffee.density,
        harvest_date=coffee.harvest_date,
        purchase_date=coffee.purchase_date,
        quantity_grams=coffee.quantity_grams,
        notes=coffee.notes,
        created_at=coffee.created_at,
        updated_at=coffee.updated_at,
    )


def profile_to_response(profile: Profile) -> ProfileResponse:
    """Convert a Profile document to ProfileResponse schema.

    Phases are stored as native subdocuments and validated into PhaseSchema.
    """
    phases = [PhaseSchema.model_validate(p) for p in profile.phases] if profile.phases else []
    return ProfileResponse(
        id=profile.id,
        name=profile.name,
        coffee_id=profile.coffee_id,
        roast_level=profile.roast_level,
        flavor_goal=profile.flavor_goal,
        brew_method=profile.brew_method,
        batch_size_grams=profile.batch_size_grams,
        phases=phases,
        target_dtr=profile.target_dtr,
        target_weight_loss=profile.target_weight_loss,
        charge_temp=profile.charge_temp,
        ai_rationale=profile.ai_rationale,
        plan_source=profile.plan_source,
        ai_model=profile.ai_model,
        warnings=profile.warnings,
        version=profile.version,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def roast_to_response(roast: Roast) -> RoastResponse:
    """Convert a Roast document to RoastResponse schema."""
    return RoastResponse(
        id=roast.id,
        profile_id=roast.profile_id,
        coffee_id=roast.coffee_id,
        batch_size_grams=roast.batch_size_grams,
        start_time=roast.start_time,
        end_time=roast.end_time,
        total_time_seconds=roast.total_time_seconds,
        charge_temp=roast.charge_temp,
        turning_point_time=roast.turning_point_time,
        turning_point_temp=roast.turning_point_temp,
        dry_end_time=roast.dry_end_time,
        dry_end_temp=roast.dry_end_temp,
        fc_start_time=roast.fc_start_time,
        fc_start_temp=roast.fc_start_temp,
        fc_end_time=roast.fc_end_time,
        fc_end_temp=roast.fc_end_temp,
        sc_start_time=roast.sc_start_time,
        sc_start_temp=roast.sc_start_temp,
        drop_time=roast.drop_time,
        drop_temp=roast.drop_temp,
        dtr=roast.dtr,
        weight_loss=roast.weight_loss,
        ambient_temp=roast.ambient_temp,
        ambient_humidity=roast.ambient_humidity,
        excluded=roast.excluded,
        status=roast.status,
        notes=roast.notes,
        bond_number=roast.bond_number,
        weight_display=roast.weight_display,
        cupping_score=roast.cupping_score,
        flavor_notes=roast.flavor_notes,
        roast_name=roast.roast_name,
        code=roast.code,
        green_weight_grams=roast.green_weight_grams,
        roasted_weight_grams=roast.roasted_weight_grams,
        bags_packed=roast.bags_packed,
        bag_weights_grams=list(roast.bag_weights_grams or []),
        bag_size_grams=roast.bag_size_grams,
        drop_id=roast.drop_id,
        green_deducted=roast.green_deducted,
        plan_id=roast.plan_id,
        control_mode=roast.control_mode,
        published=roast.published,
        created_at=roast.created_at,
    )


def roast_to_response_with_curve(roast: Roast) -> RoastWithCurve:
    """Convert a Roast document to RoastWithCurve schema.

    Curve data is stored as a native subdocument.
    """
    curve = None
    if roast.curve_data is not None:
        curve = CurveDataSchema.model_validate(roast.curve_data)

    return RoastWithCurve(
        id=roast.id,
        profile_id=roast.profile_id,
        coffee_id=roast.coffee_id,
        batch_size_grams=roast.batch_size_grams,
        start_time=roast.start_time,
        end_time=roast.end_time,
        total_time_seconds=roast.total_time_seconds,
        charge_temp=roast.charge_temp,
        turning_point_time=roast.turning_point_time,
        turning_point_temp=roast.turning_point_temp,
        dry_end_time=roast.dry_end_time,
        dry_end_temp=roast.dry_end_temp,
        fc_start_time=roast.fc_start_time,
        fc_start_temp=roast.fc_start_temp,
        fc_end_time=roast.fc_end_time,
        fc_end_temp=roast.fc_end_temp,
        sc_start_time=roast.sc_start_time,
        sc_start_temp=roast.sc_start_temp,
        drop_time=roast.drop_time,
        drop_temp=roast.drop_temp,
        dtr=roast.dtr,
        weight_loss=roast.weight_loss,
        ambient_temp=roast.ambient_temp,
        ambient_humidity=roast.ambient_humidity,
        excluded=roast.excluded,
        status=roast.status,
        notes=roast.notes,
        bond_number=roast.bond_number,
        weight_display=roast.weight_display,
        cupping_score=roast.cupping_score,
        flavor_notes=roast.flavor_notes,
        roast_name=roast.roast_name,
        code=roast.code,
        green_weight_grams=roast.green_weight_grams,
        roasted_weight_grams=roast.roasted_weight_grams,
        bags_packed=roast.bags_packed,
        bag_weights_grams=list(roast.bag_weights_grams or []),
        bag_size_grams=roast.bag_size_grams,
        drop_id=roast.drop_id,
        green_deducted=roast.green_deducted,
        plan_id=roast.plan_id,
        control_mode=roast.control_mode,
        published=roast.published,
        created_at=roast.created_at,
        curve_data=curve,
        connection_events=roast.connection_events,
    )


def cupping_to_response(cupping: Cupping) -> CuppingResponse:
    """Convert a Cupping document to CuppingResponse schema.

    Flavor notes and defects are native lists — no JSON parsing needed.
    """
    return CuppingResponse(
        id=cupping.id,
        roast_id=cupping.roast_id,
        rest_days=cupping.rest_days,
        aroma=cupping.aroma,
        acidity=cupping.acidity,
        sweetness=cupping.sweetness,
        body=cupping.body,
        aftertaste=cupping.aftertaste,
        overall=cupping.overall,
        flavor_notes=cupping.flavor_notes,
        defects=cupping.defects,
        brew_method=cupping.brew_method,
        cupper_name=cupping.cupper_name,
        notes=cupping.notes,
        next_time_notes=cupping.next_time_notes,
        cupped_at=cupping.cupped_at,
    )


__all__ = [
    "coffee_to_response",
    "cupping_to_response",
    "profile_to_response",
    "roast_to_response",
    "roast_to_response_with_curve",
]
