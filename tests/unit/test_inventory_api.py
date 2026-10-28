"""Local ASGI/simulator flows and mocked signed store requests."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from maillard.core.config import MaillardConfig
from maillard.db.models import Coffee, InventoryMove, PresaleDrop, Roast
from maillard.driver.simulator import SimulatorDriver
from maillard.inventory import service as inv
from maillard.mcp.state import ApplicationState
from maillard.mcp.tools.coffee import get_inventory_summary
from maillard.mcp.tools.execution import start_roast
from maillard.mcp.tools.roaster import execute_charge, execute_drop
from maillard.sync.site import SiteSyncClient, build_roast_payload, sign_body
from maillard.web.routes import create_router

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture
async def local_app():
    state = ApplicationState(
        config=MaillardConfig(
            poll_interval=0.1,
            ingest_secret_file=None,
            ingest_secret=None,
            zebra_host="mock-printer",
        )
    )
    sim = SimulatorDriver()
    await sim.connect("simulator")
    state.driver = sim
    app = FastAPI()
    app.state.maillard = state
    app.state.ai_agent = None
    app.include_router(create_router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, state, app
    await state.halt_heat_sources()
    for attr in ("_monitor", "_safety_monitor"):
        obj = getattr(state, attr)
        if obj:
            await obj.stop()
    state.clear_roast_context()
    await sim.disconnect()
    if hasattr(app.state, "site_sync_client"):
        await app.state.site_sync_client.aclose()


@pytest.fixture
async def coffee(mongo_db):  # noqa: ARG001 - initializes Beanie before dependent fixtures
    return await Coffee(name="Uraga", origin="Ethiopia", quantity_grams=9000).insert()


async def test_lots_summary_moves_adjust_api(local_app, coffee):
    client, _, _ = local_app
    receipt = (
        await client.post(
            "/api/inventory/lots",
            json={
                "coffee_id": coffee.id,
                "grams_in": 1500,
                "supplier": "Test",
                "idempotency_key": "receipt",
            },
        )
    ).json()
    assert receipt["grams_remaining"] == 1500
    assert "applied_moves" not in receipt
    again = (
        await client.post(
            "/api/inventory/lots",
            json={"coffee_id": coffee.id, "grams_in": 1500, "idempotency_key": "receipt"},
        )
    ).json()
    assert again["id"] == receipt["id"]
    adjusted = await client.post(
        f"/api/inventory/lots/{receipt['id']}/adjust", json={"grams": -200, "note": "count"}
    )
    assert adjusted.json()["grams_remaining"] == 1300
    rejected = await client.post(
        f"/api/inventory/lots/{receipt['id']}/adjust", json={"grams": -1400, "note": "count"}
    )
    assert rejected.status_code == 409
    assert (await client.get(f"/api/inventory/lots?coffee_id={coffee.id}")).json()[0][
        "grams_remaining"
    ] == 1300
    assert len((await client.get(f"/api/inventory/moves?coffee_id={coffee.id}")).json()) == 2
    summary = (await client.get("/api/inventory/summary")).json()[0]
    assert (summary["remaining"], summary["reserved"], summary["free"]) == (1300, 0, 1300)
    assert (await client.get(f"/api/coffees/{coffee.id}")).json()["quantity_grams"] == 1300
    assert (await Coffee.get(coffee.id)).quantity_grams == 9000
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": local_app[1]}
    assert (await get_inventory_summary(ctx))["coffees"][0]["remaining"] == 1300


async def test_unknown_and_zero_are_distinct(local_app, coffee):
    client, _, _ = local_app
    summary = (await client.get("/api/inventory/summary")).json()[0]
    assert summary["remaining"] == 0 and not summary["known"]
    await inv.physical_count(coffee.id, 0, count_id="zero", date="2026-10-03")
    assert (await client.get("/api/inventory/summary")).json()[0]["known"]


async def test_web_charge_completion_preserves_weight_and_deducts(local_app, coffee):
    client, _, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    charged = (
        await client.post("/api/charge", json={"coffee_id": coffee.id, "batch_size_grams": 1200})
    ).json()
    rid = charged["roast_id"]
    # Edit during the active session; final telemetry must preserve the measured weight.
    await client.post(f"/api/roasts/{rid}/weights", json={"green_weight_grams": 1100})
    assert (await client.post("/api/drop")).json()["status"] == "dropped"
    stored = await Roast.get(rid)
    assert stored.green_weight_grams == 1100 and stored.status == "completed"
    assert stored.green_deducted
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 400
    saved = (
        await client.post(f"/api/roasts/{rid}/weights", json={"roasted_weight_grams": 935})
    ).json()
    assert saved["bags_packed"] == 2 and saved["weight_loss"] == 15
    await client.post(
        f"/api/roasts/{rid}/weights",
        json={"roasted_weight_grams": 935, "bags_packed": 1, "bag_size_grams": 454},
    )
    assert (await Roast.get(rid)).bags_packed == 1
    assert await InventoryMove.find({"roast_id": rid, "kind": "roasted"}).count() == 1


@pytest.mark.parametrize("charge_path", ["execute_charge", "start_roast"])
async def test_mcp_charge_full_flow_green_persisted(local_app, coffee, charge_path):
    _, state, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    charge = execute_charge if charge_path == "execute_charge" else start_roast
    result = await charge(ctx, coffee_id=coffee.id, batch_size_grams=1200, confirmed=True)
    stored = await Roast.get(result["roast_id"])
    assert stored.green_weight_grams == 1200
    dropped = await execute_drop(ctx, confirmed=True)
    assert dropped["status"] == "dropped"
    stored = await Roast.get(result["roast_id"])
    assert stored.status == "completed" and stored.green_weight_grams == 1200
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 300


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize(
    "weight,bags,blocked", [(None, None, True), (900, None, True), (None, 2, True), (900, 1, False)]
)
async def test_label_gate_including_override(local_app, weight, bags, blocked, override):
    client, _, _ = local_app
    roast = await Roast(
        status="completed",
        code="K7Q2MX",
        roasted_weight_grams=weight,
        bags_packed=bags,
        site_synced_code="K7Q2MX",
        site_synced_at="2026-10-03",
        site_published=True,
    ).insert()
    with patch("maillard.web.label_service.send_to_printer") as send:
        response = await client.post(
            "/api/labels/print",
            json={"roast_id": roast.id, "override": override, "net_weight_grams": 365},
        )
    assert response.status_code == (409 if blocked else 200)
    if blocked:
        assert "roasted weight and bags packed" in response.json()["detail"]
        send.assert_not_called()
    else:
        send.assert_called_once()


async def test_late_weights_and_drop_deduct_once(local_app, coffee):
    client, _, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    drop = await inv.open_drop(coffee.id, 2)
    roast = await Roast(coffee_id=coffee.id, batch_size_grams=800, status="completed").insert()
    body = {
        "green_weight_grams": 800,
        "roasted_weight_grams": 680,
        "bags_packed": 2,
        "drop_id": drop.id,
    }
    for _ in range(2):
        result = await client.post(f"/api/roasts/{roast.id}/weights", json=body)
        assert result.status_code == 200 and result.json()["status"] == "ok"
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 0
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 700
    assert await InventoryMove.find({"roast_id": roast.id, "kind": "roasted"}).count() == 1


@pytest.mark.parametrize(
    "body",
    [
        {"bags_packed": -1},
        {"bags_packed": 1.5},
        {"bag_size_grams": 0},
        {"roasted_weight_grams": -1},
        {"green_weight_grams": 5001},
    ],
)
async def test_weights_invalid_values(local_app, body):
    roast = await Roast(status="completed").insert()
    response = await local_app[0].post(f"/api/roasts/{roast.id}/weights", json=body)
    assert response.status_code == 422


async def test_signed_payload_and_drop_status(coffee):
    await inv.receive_lot(coffee.id, 1500)
    drop = await inv.open_drop(coffee.id, 3)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["X-Maillard-Signature"] == sign_body(request.content, "test-secret")
        assert request.headers["X-Maillard-Timestamp"].isdigit()
        data = json.loads(request.content)
        assert data["schema_version"] == 1 and data["sent_at"].endswith("Z")
        if request.url.path == "/api/maillard/drops/status":
            assert data["maillard_ids"] == [drop.id]
            return httpx.Response(
                200,
                json={
                    "drops": [
                        {
                            "maillard_id": drop.id,
                            "bags_sold": 2,
                            "bags_fulfilled": 1,
                            "bags_held": 0,
                            "drop_number": 4,
                        }
                    ]
                },
            )
        if request.url.path == "/api/maillard/drops":
            assert data["drop"]["green_reserved_grams"] == 1200
            assert data["drop"]["coffee_maillard_id"] == coffee.id
            assert data["drop"]["green_grams_per_bag"] == 400
            return httpx.Response(
                200,
                json={
                    "drop": {
                        "maillard_id": drop.id,
                        "drop_number": 4,
                        "bags_sold": 0,
                        "bags_fulfilled": 0,
                    }
                },
            )
        assert data["roast"]["bags_packed"] == 2
        assert data["roast"]["bag_size_grams"] == 340
        assert data["roast"]["drop"] == {"maillard_id": drop.id, "drop_number": 4}
        return httpx.Response(200, json={})

    client = SiteSyncClient(
        MaillardConfig(ingest_secret="test-secret", site_url="https://store.invalid"),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.push_drop(drop)
        await client.fetch_drop_status([drop.id])
        stored = await PresaleDrop.get(drop.id)
        assert stored.bags_sold == 2 and stored.bags_fulfilled == 1 and stored.drop_number == 4
        roast = await Roast(
            coffee_id=coffee.id,
            status="completed",
            bags_packed=2,
            roasted_weight_grams=680,
            drop_id=drop.id,
        ).insert()
        assert (
            build_roast_payload(roast, None, coffee, version="test", drop=stored)["roast"]["drop"][
                "drop_number"
            ]
            == 4
        )
        await client.send(roast)
        assert len(requests) == 3
    finally:
        await client.aclose()


@pytest.mark.parametrize("action", ["close", "cancel"])
@pytest.mark.parametrize("store_response", [200, 409, 500])
async def test_close_cancel_store_rejection_retains_green(
    local_app, coffee, action, store_response
):
    client, _, app = local_app
    await inv.receive_lot(coffee.id, 1500)
    drop = await inv.open_drop(coffee.id, 3)
    await drop.set({"drop_number": 4})
    bags_sold = 2 if action == "close" else 0
    calls = []

    def handler(request):
        calls.append(request.url.path)
        status = {
            "maillard_id": drop.id,
            "bags_sold": bags_sold,
            "bags_held": 0,
            "bags_fulfilled": 0,
            "drop_number": 4,
        }
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"drops": [status]})
        payload = json.loads(request.content)["drop"]
        assert payload["capacity_bags"] == bags_sold
        assert payload["status"] == ("closed" if action == "close" else "cancelled")
        return httpx.Response(
            store_response,
            json={"drop": status}
            if store_response == 200
            else {"error": "capacity_below_sold", "bags_sold": bags_sold},
        )

    app.state.site_sync_client = SiteSyncClient(
        MaillardConfig(ingest_secret="secret", site_url="https://store.invalid"),
        transport=httpx.MockTransport(handler),
    )
    response = await client.post(f"/api/drops/{drop.id}/{action}")
    assert response.status_code == store_response
    balance = await inv.coffee_summary(coffee.id)
    assert balance["reserved"] == (bags_sold * 400 if store_response == 200 else 1200)
    assert len(calls) == 2


async def test_drop_crud_api_offline(local_app, coffee):
    client, _, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    failed = await client.post("/api/drops", json={"coffee_id": coffee.id, "capacity_bags": 4})
    assert failed.status_code == 409
    drop = (
        await client.post("/api/drops", json={"coffee_id": coffee.id, "capacity_bags": 3})
    ).json()
    assert (await client.get("/api/drops")).json()[0]["green_reserved_grams"] == 1200
    resize = await client.post(f"/api/drops/{drop['id']}/resize", json={"capacity_bags": 2})
    assert resize.json()["green_reserved_grams"] == 800
    sync = await client.post(f"/api/drops/{drop['id']}/sync")
    assert sync.status_code == 409
    close = await client.post(f"/api/drops/{drop['id']}/close")
    assert close.json()["status"] == "closed" and close.json()["green_reserved_grams"] == 0


async def test_synced_resize_checks_sales_before_release(local_app, coffee):
    client, _, app = local_app
    await inv.receive_lot(coffee.id, 1500)
    drop = await inv.open_drop(coffee.id, 3)
    await drop.set({"drop_number": 4})

    def handler(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200, json={"drops": [{"maillard_id": drop.id, "bags_sold": 2, "bags_held": 0}]}
            )
        return httpx.Response(409, json={"error": "capacity_below_sold", "bags_sold": 3})

    app.state.site_sync_client = SiteSyncClient(
        MaillardConfig(ingest_secret="secret", site_url="https://store.invalid"),
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.post(f"/api/drops/{drop.id}/resize", json={"capacity_bags": 1})
    ).status_code == 409
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 1200
    assert (
        await client.post(f"/api/drops/{drop.id}/resize", json={"capacity_bags": 2})
    ).status_code == 409
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 1200


async def test_publishing_timeout_blocks_offline_release(local_app, coffee):
    client, _, app = local_app
    await inv.receive_lot(coffee.id, 1500)
    drop = await inv.open_drop(coffee.id, 3)

    def timeout(request):
        raise httpx.ReadTimeout("server may have committed", request=request)

    app.state.site_sync_client = SiteSyncClient(
        MaillardConfig(ingest_secret="secret", site_url="https://store.invalid"),
        transport=httpx.MockTransport(timeout),
    )
    assert (await client.post(f"/api/drops/{drop.id}/sync")).status_code == 502
    assert (await PresaleDrop.get(drop.id)).site_sync_attempted
    app.state.site_sync_client.config.ingest_secret = None
    app.state.site_sync_client.config.ingest_secret_file = None
    assert (await client.post(f"/api/drops/{drop.id}/cancel")).status_code == 409
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 1200


async def test_emergency_stop_accounts_charged_green(local_app, coffee):
    client, _, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    charged = (
        await client.post("/api/charge", json={"coffee_id": coffee.id, "batch_size_grams": 1200})
    ).json()
    result = await client.post("/api/emergency-stop")
    assert result.json()["status"] == "emergency_stopped"
    roast = await Roast.get(charged["roast_id"])
    bts = (roast.curve_data or {}).get("bt") or []
    if bts and max(bts) < 60:
        # Cold roaster: beans never heated, green is recoverable, nothing deducted.
        assert roast.status == "aborted" and not roast.green_deducted
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 1500
    else:
        assert roast.status == "aborted" and roast.green_deducted
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 300


async def test_inventory_failure_does_not_block_drop(local_app, coffee):
    client, state, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    charged = (
        await client.post("/api/charge", json={"coffee_id": coffee.id, "batch_size_grams": 1200})
    ).json()
    with patch(
        "maillard.inventory.service.deduct_for_roast", side_effect=RuntimeError("DB unavailable")
    ):
        dropped = await client.post("/api/drop")
    assert dropped.json()["status"] == "dropped"
    assert not state.roast_context.is_active
    stored = await Roast.get(charged["roast_id"])
    assert stored.status == "completed" and not stored.green_deducted
    retry = await client.post(
        f"/api/roasts/{stored.id}/weights", json={"roasted_weight_grams": 1020}
    )
    assert retry.json()["inventory"]["deducted"]


async def test_drop_can_be_associated_after_completion_without_second_deduction(local_app, coffee):
    client, _, _ = local_app
    await inv.receive_lot(coffee.id, 1500)
    drop = await inv.open_drop(coffee.id, 2)
    charged = (
        await client.post("/api/charge", json={"coffee_id": coffee.id, "batch_size_grams": 800})
    ).json()
    await client.post("/api/drop")
    rid = charged["roast_id"]
    assert (await inv.coffee_summary(coffee.id))["remaining"] == 700
    assert (await inv.coffee_summary(coffee.id))["reserved"] == 800
    for _ in range(2):
        response = await client.post(
            f"/api/roasts/{rid}/weights",
            json={"roasted_weight_grams": 680, "bags_packed": 2, "drop_id": drop.id},
        )
        assert response.status_code == 200
    balance = await inv.coffee_summary(coffee.id)
    assert balance["remaining"] == 700 and balance["reserved"] == 0
    assert await InventoryMove.find({"roast_id": rid, "kind": "roasted"}).count() == 1
    assert await InventoryMove.find({"roast_id": rid, "kind": "released"}).count() == 1
