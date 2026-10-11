"""Ledger math, FIFO, reservations, retry recovery and physical-count reconciliation."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from maillard.db.models import (
    Coffee,
    GreenLot,
    InventoryMove,
    InventoryOperation,
    PresaleDrop,
    Roast,
)
from maillard.inventory import service as inv

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture
async def coffee(mongo_db):  # noqa: ARG001 - initializes Beanie before dependent fixtures
    return await Coffee(name="Uraga", origin="Ethiopia", quantity_grams=9000).insert()


@pytest.fixture
async def lots(coffee):
    older = await inv.receive_lot(coffee.id, 500, received_at="2025-01-01", supplier="A")
    newer = await inv.receive_lot(coffee.id, 1000, received_at="2025-02-01", supplier="B")
    return older, newer


@pytest.mark.parametrize(
    "kind,grams,delta",
    [
        ("received", 100, 100),
        ("roasted", 100, -100),
        ("adjusted", -5, -5),
        ("adjusted", 5, 5),
        ("reserved", 100, 0),
        ("released", 100, 0),
    ],
)
def test_ledger_delta(kind, grams, delta):
    assert inv.remaining_delta(kind, grams) == delta


@pytest.mark.parametrize("kind", ["received", "roasted", "reserved", "released"])
def test_magnitudes_positive(kind):
    with pytest.raises(ValidationError):
        InventoryMove(lot_id="l", coffee_id="c", kind=kind, grams=-1)


async def test_received_and_adjusted_balance(coffee):
    lot = await inv.receive_lot(coffee.id, 1000, idempotency_key="receipt:1", cost_per_kg=15)
    assert (await inv.receive_lot(coffee.id, 1000, idempotency_key="receipt:1")).id == lot.id
    lot = await inv.adjust(lot.id, -200, note="count", idempotency_key="adjust:1")
    await inv.adjust(lot.id, -200, note="count", idempotency_key="adjust:1")
    assert lot.grams_remaining == 800
    assert await InventoryMove.count() == 2
    with pytest.raises(ValueError, match="negative"):
        await inv.adjust(lot.id, -801, note="count")
    with pytest.raises(ValueError, match="note"):
        await inv.adjust(lot.id, 1, note=" ")
    await inv.adjust(lot.id, -900, note="deficit", force=True)
    summary = await inv.coffee_summary(coffee.id)
    assert summary["remaining"] == summary["free"] == -100
    assert (await Coffee.get(coffee.id)).quantity_grams == 9000


async def test_nullable_sparse_unique_index(coffee):
    for _ in range(2):
        await InventoryMove(lot_id="l", coffee_id=coffee.id, kind="adjusted", grams=0).insert()
    await InventoryMove(
        lot_id="l", coffee_id=coffee.id, kind="adjusted", grams=1, idempotency_key="unique"
    ).insert()
    with pytest.raises(DuplicateKeyError):
        await InventoryMove(
            lot_id="l", coffee_id=coffee.id, kind="adjusted", grams=1, idempotency_key="unique"
        ).insert()


async def test_fifo_splitting_and_repeat(coffee, lots):
    older, newer = lots
    roast = await Roast(
        coffee_id=coffee.id, batch_size_grams=1000, green_weight_grams=700, status="completed"
    ).insert()
    await inv.deduct_for_roast(roast)
    await inv.deduct_for_roast(roast)
    assert (await GreenLot.get(older.id)).grams_remaining == 0
    assert (await GreenLot.get(newer.id)).grams_remaining == 800
    moves = await InventoryMove.find({"roast_id": roast.id, "kind": "roasted"}).to_list()
    assert [(m.lot_id, m.grams) for m in moves] == [(older.id, 500), (newer.id, 200)]
    assert moves[0].idempotency_key == f"roast:{roast.id}"
    assert (await Roast.get(roast.id)).green_deducted


@pytest.mark.usefixtures("lots")
async def test_single_lot_roast_concurrent_retry(coffee):
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=200, status="completed").insert()
    results = await asyncio.gather(*(inv.deduct_for_roast(roast) for _ in range(4)))
    assert sum(not r.get("already_applied", False) for r in results) == 1
    assert await InventoryMove.find({"roast_id": roast.id}).count() == 1
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 1300


@pytest.mark.parametrize("stage", ["ledger", "cache"])
@pytest.mark.usefixtures("lots")
async def test_resume_interrupted_deduction(coffee, stage):
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=200, status="completed").insert()
    if stage == "ledger":
        collection = GreenLot.get_motor_collection()
        with (
            patch.object(collection, "update_one", AsyncMock(side_effect=RuntimeError("crash"))),
            pytest.raises(RuntimeError, match="crash"),
        ):
            await inv.deduct_for_roast(roast)
    else:
        with (
            patch.object(InventoryOperation, "set", AsyncMock(side_effect=RuntimeError("crash"))),
            pytest.raises(RuntimeError, match="crash"),
        ):
            await inv.deduct_for_roast(roast)
    assert await InventoryMove.find({"roast_id": roast.id}).count() == 1
    await inv.deduct_for_roast(roast)
    assert await InventoryMove.find({"roast_id": roast.id}).count() == 1
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 1300
    assert (await InventoryOperation.get(f"roast:{roast.id}")).done


@pytest.mark.usefixtures("lots")
async def test_recover_before_next_operation(coffee):
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=200, status="completed").insert()
    with (
        patch.object(InventoryOperation, "set", AsyncMock(side_effect=RuntimeError("crash"))),
        pytest.raises(RuntimeError),
    ):
        await inv.deduct_for_roast(roast)
    # A new receipt recovers the pending roast before proceeding.
    await inv.receive_lot(coffee.id, 100)
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 1400
    assert (await Roast.get(roast.id)).green_deducted


@pytest.mark.parametrize(
    "status,charged,deducted",
    [
        ("abandoned", False, False),
        ("abandoned", True, True),
        ("aborted", False, False),
        ("aborted", True, True),
        ("in_progress", True, False),
    ],
)
@pytest.mark.usefixtures("lots")
async def test_abandoned_only_charged(coffee, status, charged, deducted):
    roast = await Roast(
        coffee_id=coffee.id,
        batch_size_grams=200,
        status=status,
        green_weight_grams=200 if charged else None,
    ).insert()
    result = await inv.deduct_for_roast(roast)
    assert result["deducted"] is deducted
    assert (await inv.coffee_summary(coffee.id))["remaining"] == (1300 if deducted else 1500)


@pytest.mark.parametrize("has_green", [True, False])
async def test_shortfall_recorded_once(coffee, caplog, has_green):
    if has_green:
        await inv.receive_lot(coffee.id, 100)
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=300, status="completed").insert()
    result = await inv.deduct_for_roast(roast)
    assert result["shortfall_grams"] == (200 if has_green else 300)
    assert "shortfall" in caplog.text
    await inv.deduct_for_roast(roast)
    balance = await inv.coffee_summary(coffee.id)
    assert balance["remaining"] == (-200 if has_green else -300)
    moves = await InventoryMove.find({"roast_id": roast.id, "kind": "adjusted"}).to_list()
    assert len(moves) == 1 and moves[0].grams < 0


@pytest.mark.usefixtures("lots")
async def test_drop_reserve_close_then_roast(coffee):
    drop = await inv.open_drop(coffee.id, 3)
    assert drop.green_grams_per_bag == 400
    balance = await inv.coffee_summary(coffee.id)
    assert (balance["remaining"], balance["reserved"], balance["free"]) == (1500, 1200, 300)
    assert [lot["reserved"] for lot in balance["lots"]] == [500, 700]
    await drop.set({"bags_sold": 2})
    await inv.close_drop(drop.id)
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 800
    await inv.close_drop(drop.id)  # does not release twice
    roast = await Roast(
        coffee_id=coffee.id, green_weight_grams=800, drop_id=drop.id, status="completed"
    ).insert()
    await inv.deduct_for_roast(roast)
    await inv.deduct_for_roast(roast)
    balance = await inv.coffee_summary(coffee.id)
    assert (balance["remaining"], balance["reserved"], balance["free"]) == (700, 0, 700)
    assert (await PresaleDrop.get(drop.id)).roast_ids == [roast.id]
    assert (await PresaleDrop.get(drop.id)).status == "roasted"


async def test_release_on_reserved_lots(coffee, lots):
    first = await inv.open_drop(coffee.id, 1)  # 400 on oldest lot
    second = await inv.open_drop(coffee.id, 2)  # 100 oldest + 700 newer
    roast = await Roast(
        coffee_id=coffee.id, green_weight_grams=200, drop_id=second.id, status="completed"
    ).insert()
    await inv.deduct_for_roast(roast)
    released = await InventoryMove.find({"drop_id": second.id, "kind": "released"}).to_list()
    assert [(m.lot_id, m.grams) for m in released] == [(lots[0].id, 100), (lots[1].id, 100)]
    assert (await inv.drop_summary(first))["green_reserved_grams"] == 400
    assert (await inv.drop_summary(second))["green_reserved_grams"] == 600


@pytest.mark.usefixtures("lots")
async def test_oversell_resize_cancel(coffee):
    with pytest.raises(ValueError, match="Insufficient"):
        await inv.open_drop(coffee.id, 4)
    drop = await inv.open_drop(coffee.id, 2)
    await inv.resize_drop(drop.id, 3)
    with pytest.raises(ValueError, match="Insufficient"):
        await inv.resize_drop(drop.id, 4)
    await drop.set({"bags_sold": 1})
    with pytest.raises(ValueError, match="sold"):
        await inv.resize_drop(drop.id, 0)
    with pytest.raises(ValueError, match="cancel"):
        await inv.cancel_drop(drop.id)
    await drop.set({"bags_sold": 0, "bags_held": 1})
    with pytest.raises(ValueError, match="cancel"):
        await inv.cancel_drop(drop.id)
    await drop.set({"bags_held": 0})
    await inv.cancel_drop(drop.id)
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 0


@pytest.mark.usefixtures("lots")
async def test_concurrent_drops_cannot_overreserve(coffee):
    results = await asyncio.gather(
        inv.open_drop(coffee.id, 2), inv.open_drop(coffee.id, 2), return_exceptions=True
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 800


async def test_new_physical_count_idempotent(coffee):
    await inv.physical_count(coffee.id, 750, count_id="file", date="2026-10-03")
    await inv.physical_count(coffee.id, 750, count_id="file", date="2026-10-03")
    lots = await GreenLot.find_all().to_list()
    moves = await InventoryMove.find_all().to_list()
    assert len(lots) == len(moves) == 1
    assert lots[0].grams_in == 0 and lots[0].grams_remaining == 750
    assert moves[0].kind == "adjusted" and moves[0].note == "physical count 2026-10-03"
    assert moves[0].idempotency_key == f"count:file:{coffee.id}"


async def test_count_reduction_preserves_fifo(coffee, lots):
    await inv.physical_count(coffee.id, 200, count_id="file", date="2026-10-03")
    assert (await GreenLot.get(lots[0].id)).grams_remaining == 200
    assert (await GreenLot.get(lots[1].id)).grams_remaining == 0
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 200
    await inv.physical_count(coffee.id, 1200, count_id="new-file", date="2026-10-04")
    assert (await GreenLot.get(lots[1].id)).grams_remaining == 1000


async def test_zero_count_records_adjustment(coffee):
    await inv.physical_count(coffee.id, 0, count_id="zero", date="2026-10-03")
    move = await InventoryMove.find_one({"coffee_id": coffee.id})
    assert move.kind == "adjusted" and move.grams == 0


async def test_deficit_reduces_free_for_new_drop(coffee):
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=200, status="completed").insert()
    await inv.deduct_for_roast(roast)
    await inv.receive_lot(coffee.id, 500)
    assert (await inv.coffee_summary(coffee.id))["free"] == 300
    with pytest.raises(ValueError, match="Insufficient"):
        await inv.open_drop(coffee.id, 1)


@pytest.mark.usefixtures("lots")
async def test_recover_before_journal_insert(coffee):
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=200, status="completed").insert()
    with (
        patch.object(InventoryOperation, "insert", AsyncMock(side_effect=RuntimeError("crash"))),
        pytest.raises(RuntimeError),
    ):
        await inv.deduct_for_roast(roast)
    assert await InventoryOperation.get(f"roast:{roast.id}") is None
    assert await InventoryMove.find({"roast_id": roast.id}).count() == 0
    await inv.deduct_for_roast(roast)
    assert await InventoryMove.find({"roast_id": roast.id}).count() == 1
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 1300


@pytest.mark.usefixtures("lots")
async def test_stale_drop_plan_cannot_reopen_closed_drop(coffee):
    drop = await inv.open_drop(coffee.id, 2, idempotency_key="open-drop")
    original = await InventoryOperation.get("open-drop")
    await drop.set({"bags_sold": 1})
    await inv.close_drop(drop.id)
    async with inv._locked(coffee.id):
        await inv._apply(original)
    stored = await PresaleDrop.get(drop.id)
    assert stored.status == "closed" and stored.capacity_bags == 1 and stored.bags_sold == 1
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 400


async def test_idempotency_key_cannot_cross_coffees(coffee):
    await inv.receive_lot(coffee.id, 500, idempotency_key="receipt")
    other = await Coffee(name="Other", origin="C").insert()
    with pytest.raises(ValueError, match="Idempotency"):
        await inv.receive_lot(other.id, 500, idempotency_key="receipt")


@pytest.mark.usefixtures("lots")
async def test_pure_fifo_respects_free_reservations(coffee):
    lots = await inv._lots(coffee.id)
    allocations, shortage = inv.allocate_fifo(list(reversed(lots)), 600, {lots[0].id: 400})
    assert allocations == [(lots[0].id, 100), (lots[1].id, 500)]
    assert shortage == 0
