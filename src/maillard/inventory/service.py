"""Recoverable inventory writes on standalone MongoDB (no replica set required).

A per-coffee renewable Mongo lease serializes allocation. A durable plan precedes
all mutations. Each cache increment atomically records its ledger move id; retries
insert missing moves and apply missing increments, never rewrite ledger entries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from maillard.db.models import (
    Coffee,
    GreenLot,
    InventoryMove,
    InventoryOperation,
    PresaleDrop,
    Roast,
    utc_now,
)

_logger = logging.getLogger(__name__)
_lease: ContextVar[tuple[str, str] | None] = ContextVar("inventory_lease", default=None)


def remaining_delta(kind: str, grams: float) -> float:
    return {"received": grams, "adjusted": grams, "roasted": -grams, "reserved": 0, "released": 0}[
        kind
    ]


def reservation_balances(
    moves: list[InventoryMove], drop_id: str | None = None
) -> dict[str, float]:
    balances: dict[str, float] = {}
    for move in moves:
        if drop_id is not None and move.drop_id != drop_id:
            continue
        if move.kind in ("reserved", "released"):
            balances[move.lot_id] = balances.get(move.lot_id, 0) + (
                move.grams if move.kind == "reserved" else -move.grams
            )
    return balances


def allocate_fifo(
    lots: list[GreenLot], grams: float, reserved: dict[str, float] | None = None
) -> tuple[list[tuple[str, float]], float]:
    """Pure allocation: ISO timestamps normalized on receipt; id breaks ties."""
    allocations = []
    requested = grams
    if reserved is not None:
        # Deficit lots and forced adjustments count against aggregate free green.
        grams = min(
            grams, max(0, sum(lot.grams_remaining for lot in lots) - sum(reserved.values()))
        )
    for lot in sorted(lots, key=lambda lot: (lot.received_at, lot.id)):
        available = max(0, lot.grams_remaining - (reserved or {}).get(lot.id, 0))
        take = min(available, grams)
        if take > 1e-8:
            allocations.append((lot.id, take))
            grams -= take
        if grams < 1e-8:
            break
    return allocations, max(0, requested - sum(take for _, take in allocations))


def normalize_date(value: str | None) -> str:
    if value is None:
        return utc_now()
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.astimezone(UTC).isoformat()


@asynccontextmanager
async def _locked(coffee_id: str):
    """Cross-process lease; abandoned operations recovered before new allocation."""
    locks = GreenLot.get_motor_collection().database["inventory_locks"]
    owner = str(uuid4())
    deadline = time.monotonic() + 15
    while True:
        try:
            result = await locks.update_one(
                {
                    "_id": coffee_id,
                    "$or": [{"expires": {"$lte": time.time()}}, {"expires": {"$exists": False}}],
                },
                {"$set": {"owner": owner, "expires": time.time() + 60}},
                upsert=True,
            )
            if result.matched_count or result.upserted_id:
                break
        except DuplicateKeyError:
            pass
        if time.monotonic() >= deadline:
            raise ValueError("Inventory busy; retry shortly")
        await asyncio.sleep(0.02)

    async def renew():
        while True:
            await asyncio.sleep(10)
            result = await locks.update_one(
                {"_id": coffee_id, "owner": owner, "expires": {"$gt": time.time()}},
                {"$set": {"expires": time.time() + 60}},
            )
            if result.matched_count != 1:
                raise RuntimeError("Inventory lease lost")

    task = asyncio.create_task(renew())
    token = _lease.set((coffee_id, owner))
    try:
        # A plan is atomically attached to the lease BEFORE its journal insert.
        # A new owner can recover it even if its predecessor died before insertion.
        locked = await locks.find_one({"_id": coffee_id, "owner": owner})
        if locked.get("active_operation"):
            pending = locked["active_operation"]
            pending["plan"]["_sequence"] = locked["sequence"]
            with suppress(DuplicateKeyError):
                await InventoryOperation.get_motor_collection().insert_one(pending)
        for operation in (
            await InventoryOperation.find({"coffee_id": coffee_id, "done": False})
            .sort("created_at")
            .to_list()
        ):
            await _apply(operation)
        yield
        if task.done():
            task.result()
    finally:
        _lease.reset(token)
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        await locks.update_one(
            {"_id": coffee_id, "owner": owner}, {"$set": {"owner": None, "expires": 0}}
        )


async def _assert_lease(coffee_id: str) -> tuple[Any, str]:
    lease = _lease.get()
    if lease is None or lease[0] != coffee_id:
        raise RuntimeError("Inventory mutation requires a coffee lease")
    locks = GreenLot.get_motor_collection().database["inventory_locks"]
    if (
        await locks.find_one({"_id": coffee_id, "owner": lease[1], "expires": {"$gt": time.time()}})
        is None
    ):
        raise ValueError("Inventory lease lost; retry to recover")
    return locks, lease[1]


def _plan() -> dict:
    return {"lots": [], "moves": [], "drop": None, "roast": None}


def _move(
    plan: dict,
    lot_id: str,
    coffee_id: str,
    kind: str,
    grams: float,
    *,
    key: str,
    roast_id: str | None = None,
    drop_id: str | None = None,
    note: str | None = None,
    actor: str = "operator",
) -> None:
    if abs(grams) < 1e-8 and kind != "adjusted":
        return
    move = InventoryMove(
        lot_id=lot_id,
        coffee_id=coffee_id,
        kind=kind,
        grams=grams,
        idempotency_key=key,
        roast_id=roast_id,
        drop_id=drop_id,
        note=note,
        actor=actor,
    )
    plan["moves"].append(move.model_dump(by_alias=True, exclude_none=True))


async def _apply(operation: InventoryOperation) -> None:
    plan = operation.plan
    lots = GreenLot.get_motor_collection()
    for lot in plan["lots"]:
        await _assert_lease(operation.coffee_id)
        await lots.update_one({"_id": lot["_id"]}, {"$setOnInsert": lot}, upsert=True)
    for move in plan["moves"]:
        await _assert_lease(operation.coffee_id)
        try:
            await InventoryMove.get_motor_collection().insert_one(move.copy())
        except DuplicateKeyError:
            existing = await InventoryMove.get_motor_collection().find_one({"_id": move["_id"]})
            if existing is None:
                raise
        await _assert_lease(operation.coffee_id)
        delta = remaining_delta(move["kind"], move["grams"])
        await lots.update_one(
            {"_id": move["lot_id"], "applied_moves": {"$ne": move["_id"]}},
            {"$inc": {"grams_remaining": delta}, "$addToSet": {"applied_moves": move["_id"]}},
        )
    if plan["drop"]:
        data = plan["drop"]
        await _assert_lease(operation.coffee_id)
        drops = PresaleDrop.get_motor_collection()
        await drops.update_one(
            {"_id": data["_id"]},
            {"$setOnInsert": {**data, "_inventory_sequence": plan["_sequence"]}},
            upsert=True,
        )
        # Older or repeated plans must not overwrite a newer lifecycle/sales update.
        await drops.update_one(
            {
                "_id": data["_id"],
                "$or": [
                    {"_inventory_sequence": {"$lt": plan["_sequence"]}},
                    {"_inventory_sequence": {"$exists": False}},
                ],
            },
            {"$set": {**data, "_inventory_sequence": plan["_sequence"]}},
        )

    if plan["roast"]:
        data = plan["roast"]
        await Roast.get_motor_collection().update_one(
            {"_id": data["id"]}, {"$set": {"green_deducted": True}}
        )
    locks, owner = await _assert_lease(operation.coffee_id)
    await operation.set({"done": True})
    await locks.update_one(
        {"_id": operation.coffee_id, "owner": owner, "active_operation._id": operation.id},
        {"$unset": {"active_operation": ""}},
    )


async def _execute(coffee_id: str, key: str, plan: dict) -> None:
    operation = InventoryOperation(id=key, coffee_id=coffee_id, plan=plan)
    locks, owner = await _assert_lease(coffee_id)
    locked = await locks.find_one_and_update(
        {"_id": coffee_id, "owner": owner, "expires": {"$gt": time.time()}},
        {
            "$set": {"active_operation": operation.model_dump(by_alias=True)},
            "$inc": {"sequence": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if locked is None:
        raise ValueError("Inventory lease lost; retry to recover")
    operation.plan["_sequence"] = locked["sequence"]
    await operation.insert()
    await _apply(operation)


async def _exists(key: str) -> bool:
    return await InventoryOperation.get(key) is not None


async def _lots(coffee_id: str) -> list[GreenLot]:
    return await GreenLot.find(GreenLot.coffee_id == coffee_id).sort("received_at", "_id").to_list()


async def _moves(coffee_id: str) -> list[InventoryMove]:
    return await InventoryMove.find(InventoryMove.coffee_id == coffee_id).to_list()


async def receive_lot(
    coffee_id: str,
    grams: float,
    *,
    supplier: str | None = None,
    cost_per_kg: float | None = None,
    received_at: str | None = None,
    notes: str | None = None,
    actor: str = "operator",
    idempotency_key: str | None = None,
) -> GreenLot:
    if await Coffee.get(coffee_id) is None:
        raise ValueError("Coffee not found")
    lot = GreenLot(
        coffee_id=coffee_id,
        grams_in=grams,
        supplier=supplier,
        cost_per_kg=cost_per_kg,
        received_at=normalize_date(received_at),
        notes=notes,
    )
    key = idempotency_key or f"receive:{lot.id}"
    async with _locked(coffee_id):
        previous = await InventoryOperation.get(key)
        if previous:
            if previous.coffee_id != coffee_id or not previous.plan["lots"]:
                raise ValueError("Idempotency key already used for another operation")
            return await GreenLot.get(previous.plan["lots"][0]["_id"])
        plan = _plan()
        plan["lots"].append(lot.model_dump(by_alias=True))
        _move(plan, lot.id, coffee_id, "received", grams, key=key, actor=actor, note=notes)
        await _execute(coffee_id, key, plan)
    return await GreenLot.get(lot.id)


async def adjust(
    lot_id: str,
    grams: float,
    *,
    note: str,
    force: bool = False,
    actor: str = "operator",
    idempotency_key: str | None = None,
) -> GreenLot:
    if not note.strip():
        raise ValueError("Adjustment requires a note")
    lot = await GreenLot.get(lot_id)
    if lot is None:
        raise ValueError("Lot not found")
    key = idempotency_key or f"adjust:{uuid4()}"
    async with _locked(lot.coffee_id):
        previous = await InventoryOperation.get(key)
        if previous and (
            previous.coffee_id != lot.coffee_id
            or not previous.plan["moves"]
            or previous.plan["moves"][0]["kind"] != "adjusted"
            or previous.plan["moves"][0]["lot_id"] != lot_id
        ):
            raise ValueError("Idempotency key already used for another operation")
        if previous is None:
            lot = await GreenLot.get(lot_id)
            if lot.grams_remaining + grams < -1e-8 and not force:
                raise ValueError("Adjustment would make remaining green negative; use force")
            plan = _plan()
            _move(plan, lot_id, lot.coffee_id, "adjusted", grams, key=key, note=note, actor=actor)
            await _execute(lot.coffee_id, key, plan)
    return await GreenLot.get(lot_id)


async def coffee_summary(coffee_id: str) -> dict:
    lots = await _lots(coffee_id)
    reserved = reservation_balances(await _moves(coffee_id))
    rows = [
        dict(
            lot.model_dump(),
            reserved=reserved.get(lot.id, 0),
            free=lot.grams_remaining - reserved.get(lot.id, 0),
        )
        for lot in lots
    ]
    remaining = sum(lot.grams_remaining for lot in lots)
    held = sum(reserved.values())
    return {
        "coffee_id": coffee_id,
        "remaining": remaining,
        "reserved": held,
        "free": remaining - held,
        "known": bool(lots),
        "lots": rows,
    }


async def inventory_summary() -> list[dict]:
    return [
        dict(await coffee_summary(coffee.id), coffee_name=coffee.name)
        for coffee in await Coffee.find_all().sort("name").to_list()
    ]


async def _release_for_drop(
    plan: dict, drop: PresaleDrop, grams: float, key: str, roast_id: str
) -> None:
    reserved = reservation_balances(await _moves(drop.coffee_id), drop.id)
    for lot in await _lots(drop.coffee_id):
        take = min(grams, max(0, reserved.get(lot.id, 0)))
        _move(
            plan,
            lot.id,
            drop.coffee_id,
            "released",
            take,
            key=f"{key}:release:{lot.id}",
            roast_id=roast_id,
            drop_id=drop.id,
        )
        grams -= take
    if roast_id not in drop.roast_ids:
        drop.roast_ids.append(roast_id)
    drop.status = "roasted"
    plan["drop"] = drop.model_dump(by_alias=True, exclude={"green_grams_per_bag"})


def _roast_was_charged(roast: Roast) -> bool:
    """False only for an aborted false start in a cold roaster. The batch weight is
    pre-filled at Charge, so e.g. the Oct 16 abort (9.5 s, BT 22-23 C) consumed 924 g
    of green that never went in. Without curve evidence we keep deducting (safe default,
    e.g. an emergency stop right after charge)."""
    if roast.roasted_weight_grams or roast.bags_packed:
        return True
    curve = roast.curve_data or {}
    bts = [b for b in (curve.get("bt") or []) if isinstance(b, (int, float))]
    return not bts or max(bts) >= 60


async def deduct_for_roast(roast: Roast) -> dict:
    key = f"roast:{roast.id}"
    if not roast.coffee_id or roast.status not in ("completed", "aborted", "abandoned"):
        return {"deducted": False, "warning": "Roast unfinished or coffee unknown"}
    if roast.status in ("aborted", "abandoned") and (
        roast.green_weight_grams is None or not _roast_was_charged(roast)
    ):
        return {"deducted": False, "warning": "Uncharged roast"}
    grams = (
        roast.green_weight_grams if roast.green_weight_grams is not None else roast.batch_size_grams
    )
    if grams is None or grams <= 0:
        return {"deducted": False, "warning": "Green weight unknown"}
    async with _locked(roast.coffee_id):
        previous = await InventoryOperation.get(key)
        if previous:
            accounted_drop = previous.plan["roast"].get("drop_id")
            if roast.drop_id and roast.drop_id != accounted_drop:
                if accounted_drop:
                    raise ValueError("Roast already accounted against another drop")
                attachment_key = f"{key}:drop"
                attachment = await InventoryOperation.get(attachment_key)
                if attachment and attachment.plan["roast"]["drop_id"] != roast.drop_id:
                    raise ValueError("Roast already linked to another drop")
                if attachment is None:
                    drop = await PresaleDrop.get(roast.drop_id)
                    if (
                        drop is None
                        or drop.coffee_id != roast.coffee_id
                        or drop.status == "cancelled"
                    ):
                        raise ValueError("Drop must belong to the roast coffee and be active")
                    plan = _plan()
                    used = previous.plan["roast"]["grams"]
                    await _release_for_drop(plan, drop, used, attachment_key, roast.id)
                    plan["roast"] = {"id": roast.id, "grams": used, "drop_id": drop.id}
                    await _execute(roast.coffee_id, attachment_key, plan)
            roast.green_deducted = True
            return {"deducted": True, "already_applied": True}
        lots = await _lots(roast.coffee_id)
        plan = _plan()
        drop = await PresaleDrop.get(roast.drop_id) if roast.drop_id else None
        if roast.drop_id and (drop is None or drop.coffee_id != roast.coffee_id):
            raise ValueError("Drop must belong to the roast coffee")
        if drop:
            await _release_for_drop(plan, drop, grams, key, roast.id)
        allocations, shortfall = allocate_fifo(lots, grams)
        for index, (lot_id, take) in enumerate(allocations):
            _move(
                plan,
                lot_id,
                roast.coffee_id,
                "roasted",
                take,
                key=key if index == 0 else f"{key}:lot:{lot_id}",
                roast_id=roast.id,
                drop_id=roast.drop_id,
                actor="roast",
            )
        if shortfall > 1e-8:
            # An explicit deficit lot keeps ledger math honest and operator-visible.
            deficit = GreenLot(
                coffee_id=roast.coffee_id,
                grams_in=0,
                notes=f"Unaccounted green for roast {roast.id}",
            )
            plan["lots"].append(deficit.model_dump(by_alias=True))
            _move(
                plan,
                deficit.id,
                roast.coffee_id,
                "adjusted",
                -shortfall,
                key=f"{key}:shortfall" if allocations else key,
                roast_id=roast.id,
                note=f"Roast shortfall: {shortfall:g} g; reconcile physical inventory",
                actor="roast",
            )
            _logger.warning("Roast %s green inventory shortfall: %s g", roast.id, shortfall)
        plan["roast"] = {"id": roast.id, "grams": grams, "drop_id": roast.drop_id}
        await _execute(roast.coffee_id, key, plan)
        roast.green_deducted = True
        return {"deducted": True, "shortfall_grams": shortfall}


async def open_drop(
    coffee_id: str,
    capacity_bags: int,
    *,
    bag_size_grams: float = 340,
    expected_yield: float = 0.85,
    idempotency_key: str | None = None,
) -> PresaleDrop:
    if await Coffee.get(coffee_id) is None:
        raise ValueError("Coffee not found")
    drop = PresaleDrop(
        coffee_id=coffee_id,
        capacity_bags=capacity_bags,
        bag_size_grams=bag_size_grams,
        expected_yield=expected_yield,
    )
    key = idempotency_key or f"drop:{drop.id}:open"
    async with _locked(coffee_id):
        previous = await InventoryOperation.get(key)
        if previous:
            if previous.coffee_id != coffee_id or not previous.plan["drop"]:
                raise ValueError("Idempotency key already used for another operation")
            return await PresaleDrop.get(previous.plan["drop"]["_id"])
        allocations, shortage = allocate_fifo(
            await _lots(coffee_id),
            capacity_bags * drop.green_grams_per_bag,
            reservation_balances(await _moves(coffee_id)),
        )
        if shortage > 1e-8:
            raise ValueError("Insufficient free green for drop capacity")
        plan = _plan()
        for lot_id, grams in allocations:
            _move(
                plan, lot_id, coffee_id, "reserved", grams, key=f"{key}:{lot_id}", drop_id=drop.id
            )
        plan["drop"] = drop.model_dump(by_alias=True, exclude={"green_grams_per_bag"})
        await _execute(coffee_id, key, plan)
    return await PresaleDrop.get(drop.id)


async def resize_drop(
    drop_id: str, capacity_bags: int, *, status: str | None = None
) -> PresaleDrop:
    drop = await PresaleDrop.get(drop_id)
    if drop is None:
        raise ValueError("Drop not found")
    async with _locked(drop.coffee_id):
        drop = await PresaleDrop.get(drop_id)
        status = status or drop.status
        if status not in ("open", "closed", "cancelled"):
            raise ValueError("Invalid drop status")
        if drop.status == "closed" and status == "open":
            raise ValueError("Cannot reopen a closed drop")
        if drop.status in ("cancelled", "roasted"):
            raise ValueError("Drop already cancelled or roasted")
        if status == "cancelled" and (drop.bags_sold or drop.bags_held):
            raise ValueError("Cannot cancel a drop with sold bags or active holds")
        if capacity_bags < drop.bags_sold + drop.bags_held:
            raise ValueError("Capacity below sold bags plus active holds")
        if capacity_bags < 0:
            raise ValueError("Capacity must be nonnegative")
        moves = await _moves(drop.coffee_id)
        reserved = reservation_balances(moves, drop.id)
        delta = capacity_bags * drop.green_grams_per_bag - sum(reserved.values())
        plan, key = _plan(), f"drop:{drop.id}:{uuid4()}"
        lots = await _lots(drop.coffee_id)
        if delta > 1e-8:
            allocations, shortage = allocate_fifo(lots, delta, reservation_balances(moves))
            if shortage > 1e-8:
                raise ValueError("Insufficient free green for drop capacity")
            kind = "reserved"
        else:
            remaining = -delta
            allocations = []
            # Release newest allocations first, preserving the oldest FIFO reservation.
            for lot in reversed(lots):
                take = min(remaining, max(0, reserved.get(lot.id, 0)))
                if take > 1e-8:
                    allocations.append((lot.id, take))
                remaining -= take
            kind = "released"
        for lot_id, grams in allocations:
            _move(plan, lot_id, drop.coffee_id, kind, grams, key=f"{key}:{lot_id}", drop_id=drop.id)
        drop.capacity_bags, drop.status = capacity_bags, status
        if status in ("closed", "cancelled"):
            drop.closed_at = drop.closed_at or utc_now()
        plan["drop"] = drop.model_dump(by_alias=True, exclude={"green_grams_per_bag"})
        await _execute(drop.coffee_id, key, plan)
    return await PresaleDrop.get(drop_id)


async def close_drop(drop_id: str) -> PresaleDrop:
    drop = await PresaleDrop.get(drop_id)
    if drop is None:
        raise ValueError("Drop not found")
    return await resize_drop(drop_id, drop.bags_sold, status="closed")


async def cancel_drop(drop_id: str) -> PresaleDrop:
    return await resize_drop(drop_id, 0, status="cancelled")


async def apply_drop_status(data: dict) -> None:
    """Store owns sales and fulfilment counts; update only those fields."""
    drop = await PresaleDrop.get(data.get("maillard_id"))
    if drop is None:
        return
    async with _locked(drop.coffee_id):
        updates = {
            key: data[key]
            for key in ("drop_number", "bags_sold", "bags_held", "bags_fulfilled")
            if key in data
        }
        candidate = drop.model_copy(update=updates)
        PresaleDrop.model_validate(candidate.model_dump())
        await drop.set(updates)


async def drop_summary(drop: PresaleDrop) -> dict:
    moves = await _moves(drop.coffee_id)
    return dict(
        drop.model_dump(), green_reserved_grams=sum(reservation_balances(moves, drop.id).values())
    )


async def coffee_api_response(coffee: Coffee) -> dict:
    from maillard.db.converters import coffee_to_response

    data = coffee_to_response(coffee).model_dump()
    balance = await coffee_summary(coffee.id)
    data.update(quantity_grams=balance["remaining"], inventory_known=balance["known"])
    return data


async def deduct_finished_roast(roast: Roast) -> None:
    """Inventory failure must never block cooling or operator drop controls."""
    try:
        await deduct_for_roast(roast)
    except Exception:
        _logger.exception("Inventory deduction pending for roast %s; retry via weights", roast.id)


async def physical_count(
    coffee_id: str,
    grams: float,
    *,
    count_id: str,
    date: str,
    supplier: str | None = None,
    cost_per_kg: float | None = None,
    received_at: str | None = None,
) -> None:
    """Reconcile an absolute count; reductions use newest lots first to preserve FIFO."""
    import math

    if not math.isfinite(grams) or grams < 0:
        raise ValueError("Physical count must be finite and nonnegative")
    if await Coffee.get(coffee_id) is None:
        raise ValueError("Coffee not found")
    key = f"count:{count_id}:{coffee_id}"
    async with _locked(coffee_id):
        if await _exists(key):
            return
        lots = await _lots(coffee_id)
        plan = _plan()
        if not lots:
            lot = GreenLot(
                coffee_id=coffee_id,
                grams_in=0,
                supplier=supplier,
                cost_per_kg=cost_per_kg,
                received_at=normalize_date(received_at),
            )
            lots = [lot]
            plan["lots"].append(lot.model_dump(by_alias=True))
        delta = grams - sum(lot.grams_remaining for lot in lots)
        note = f"physical count {date}"
        if delta >= 0:
            _move(
                plan,
                lots[-1].id,
                coffee_id,
                "adjusted",
                delta,
                key=key,
                note=note,
                actor="physical-count",
            )
        else:
            reduction = -delta
            for index, lot in enumerate(reversed(lots)):
                take = min(reduction, max(0, lot.grams_remaining))
                if take > 1e-8:
                    _move(
                        plan,
                        lot.id,
                        coffee_id,
                        "adjusted",
                        -take,
                        key=key if not plan["moves"] else f"{key}:{index}",
                        note=note,
                        actor="physical-count",
                    )
                    reduction -= take
        await _execute(coffee_id, key, plan)


async def mark_drop_sync_attempted(drop_id: str) -> None:
    drop = await PresaleDrop.get(drop_id)
    if drop is None:
        raise ValueError("Drop not found")
    async with _locked(drop.coffee_id):
        await drop.set({"site_sync_attempted": True})
