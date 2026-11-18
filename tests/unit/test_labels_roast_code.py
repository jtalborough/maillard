"""Per-roast label readiness and mocked print transports."""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from maillard.core.config import MaillardConfig
from maillard.db.models import Roast, RoastPlan, SyncOutbox
from maillard.driver.simulator import SimulatorDriver
from maillard.mcp.state import ApplicationState
from maillard.web.routes import create_router


@pytest.fixture
async def label_app(mongo_db):  # noqa: ARG001
    state = ApplicationState(config=MaillardConfig(zebra_host="mock-printer", label_base_url=""))
    sim = SimulatorDriver()
    await sim.connect("simulator")
    state.driver = sim
    app = FastAPI()
    app.state.maillard = state
    app.include_router(create_router())
    with patch("maillard.web.label_service.socket.socket"), patch("maillard.web.label_service.subprocess.run") as lp, patch("maillard.web.label_service.send_to_printer") as send:
        lp.return_value.returncode = 0
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, state, lp, send
    await sim.disconnect()


@pytest.mark.asyncio
async def test_readiness_and_print(label_app):
    client, state, lp, send = label_app
    plan = RoastPlan(name="Test plan", planned_curve={"time_seconds": [0, 600], "bt": [180, 210]})
    await plan.insert()
    roast = Roast(code="K7Q2MX", status="completed", plan_id=plan.id,
                  curve_data={"time_seconds": [0, 600], "bt": [190, 208]}, bond_number=12,
                  roasted_weight_grams=1020, bags_packed=8)
    await roast.insert()
    url = f"/api/labels/print-readiness/{roast.id}"
    payload = {"roast_id": roast.id, "net_weight_grams": 365}
    ready = (await client.get(url)).json()
    assert ready["qr_url"] == "https://roastery.example.com/r/K7Q2MX"
    assert ready["code"] == "K7Q2MX" and ready["printer"] == "tcp"
    assert "published" in ready and "warnings" in ready
    assert not ready["sync_ok"]
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png") as render:
        preview = (await client.post("/api/labels/preview", json=payload)).json()
        data = render.call_args.args[0]
        assert data.short_url == "roastery.example.com/r/K7Q2MX"
        assert data.roast_code == "K7Q2MX"
        assert not preview["sync_ok"] and preview["qr_url"] == ready["qr_url"]
    assert (await client.post("/api/labels/print", json=payload)).json()["code"] == "sync_required"
    send.assert_not_called()
    assert (await client.post("/api/labels/print", json={**payload, "override": True})).json()["status"] == "ok"
    for published in [False, None, True]:
        await roast.set({"site_synced_at": "2026-10-01", "site_synced_code": roast.code,
                         "site_published": published, "site_warnings": ["test warning"]})
        result = (await client.post("/api/labels/print", json=payload)).json()
        assert result.get("code") == (None if published is True else "not_published")
        assert (await client.get(url)).json()["sync_ok"] is (published is True)
    await roast.set({"code": "NEW123"})
    assert (await client.post("/api/labels/print", json=payload)).json()["code"] == "sync_required"
    state.config.zebra_cups_name = "mock-cups"
    state.config.zebra_transport = "tcp"
    lp.reset_mock()
    result = (await client.post("/api/labels/print", json={**payload, "override": True})).json()
    assert result["printer"] == "tcp"
    lp.assert_not_called()
    state.config.zebra_transport = "auto"
    lp.side_effect = FileNotFoundError("lp")
    result = (await client.post("/api/labels/print", json={**payload, "override": True})).json()
    assert result["fallback"] and result["printer"] == "tcp"
    send.assert_called()
    assert (await client.put(f"/api/roasts/{roast.id}", json={"published": True})).json()["published"]
    for _ in range(10):
        await asyncio.sleep(0)
        item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id)
        if item:
            break
    assert item and item.status == "pending"


@pytest.mark.asyncio
async def test_legacy_base_and_code_assignment(label_app, caplog):
    client, state, _lp, _send = label_app
    state.config.label_base_url = "https://www.example.org/"
    state.config.zebra_transport = "cups"
    roast = Roast(status="completed")
    await roast.insert()
    ready = (await client.get(f"/api/labels/print-readiness/{roast.id}")).json()
    assert ready["code"]
    assert ready["qr_url"] == f"https://www.example.org/r/{ready['code']}"
    assert ready["printer"] is None
    assert "deprecated" in caplog.text
    saved = await Roast.get(roast.id)
    assert saved.code == ready["code"]
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png") as render:
        await client.post("/api/labels/preview", json={"roast_id": roast.id})
        assert render.call_args.args[0].short_url == f"example.org/r/{ready['code']}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saved_number", "saved_series", "override", "effective"),
    [
        (12, "RB", None, (12, "RB")),
        (12, "RB", "  ", (12, "RB")),
        (12, "RB", "xc014", (14, "XC")),
        (12, "RB", 7, (7, None)),
        (0, None, "", (0, None)),
        (None, None, "", (None, None)),
    ],
)
async def test_label_drop_override_and_stable_reprint(
    label_app, saved_number, saved_series, override, effective
):
    client, state, lp, send = label_app
    roast = await Roast(
        code="K7Q2MX", status="completed", bond_number=saved_number, drop_series=saved_series,
        drop_id="sample-drop", roasted_weight_grams=1000, bags_packed=2,
    ).insert()
    body = {
        "roast_id": roast.id, "style": "drop", "override": True, "net_weight_grams": 365,
        "overrides": {"drop_label": override},
    }
    with (
        patch("maillard.web.label_service.build_preview_png", return_value=b"png") as preview,
        patch("maillard.web.label_service.build_zpl", return_value="^XA^XZ") as zpl,
    ):
        assert (await client.post("/api/labels/preview", json=body)).json()["status"] == "ok"
        expected = preview.call_args.args[0]
        assert (expected.bond_number, expected.drop_series) == effective
        printed = await client.post("/api/labels/print", json=body)
        assert printed.json()["message"] == "Label data sent over TCP"
        assert zpl.call_args.args[0] == expected
        send.assert_called_once()
        prerun = await Roast.get(roast.id)
        assert (prerun.bond_number, prerun.drop_series, prerun.code, prerun.drop_id) == (
            saved_number, saved_series, "K7Q2MX", "sample-drop",
        )
        assert await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id) is None

        await roast.set({"bond_number": 99, "drop_series": "NEW"})
        state.config.zebra_cups_name = "mock-cups"
        reprint = {**body, "style": "bond", "reprint": True, "overrides": {}, "net_weight_grams": 370}
        assert (await client.post("/api/labels/preview", json=reprint)).json()["status"] == "ok"
        last = preview.call_args.args[0]
        assert (last.bond_number, last.drop_series) == effective
        assert last.weight_display == "NET WT 12.8 OZ (365 g)"
        result = (await client.post("/api/labels/print", json=reprint)).json()
        assert result["message"] == "Label job queued in CUPS"
        assert result["bag_weights_grams"] == [365]
        assert preview.call_args.args[0] == last
        lp.assert_called_once()
        assert (await Roast.get(roast.id)).last_label_drop == prerun.last_label_drop


@pytest.mark.asyncio
async def test_legacy_label_reprints_from_saved_roast_drop(label_app, mongo_db):
    client, _state, _lp, _send = label_app
    await mongo_db.roasts.insert_one({
        "_id": "legacy", "code": "K7Q2MX", "status": "completed", "bond_number": 27,
        "roasted_weight_grams": 500, "bags_packed": 1, "bag_weights_grams": [340],
    })
    with patch("maillard.web.label_service.build_zpl", return_value="^XA^XZ") as zpl:
        response = await client.post("/api/labels/print", json={
            "roast_id": "legacy", "reprint": True, "override": True,
        })
    assert response.json()["status"] == "ok"
    assert zpl.call_args.args[0].bond_number == 27
    assert zpl.call_args.args[0].weight_display == "11.9 OZ (340 g)"
    record = await mongo_db.roasts.find_one({"_id": "legacy"})
    assert record["bag_weights_grams"] == [340]
    assert record["bond_number"] == 27
    assert "last_label_drop" not in record


@pytest.mark.asyncio
async def test_failed_transport_and_invalid_overrides_do_not_record_a_bag(label_app):
    client, state, lp, send = label_app
    roast = await Roast(code="K7Q2MX", status="completed").insert()
    body = {"roast_id": roast.id, "override": True, "net_weight_grams": 365}
    print_url = "/api/labels/print"
    assert (await client.post(print_url, json=body)).status_code == 409
    await roast.set({"roasted_weight_grams": 500, "bags_packed": 1})
    for invalid in [0, "RB000", "RB-001", "1234567", True, 3.5, ["RB001"]]:
        bad = {**body, "overrides": {"drop_label": invalid}}
        for endpoint in ("preview", "print"):
            assert (await client.post(f"/api/labels/{endpoint}", json=bad)).status_code == 422
    assert (await client.post(print_url, json={**body, "reprint": True})).status_code == 409
    send.assert_not_called()
    with patch("maillard.web.label_service.build_zpl", return_value="^XA^XZ"):
        send.side_effect = ConnectionRefusedError("private destination")
        refused = await client.post(print_url, json=body)
        assert refused.status_code == 502
        assert "Printer submission failed" in refused.json()["detail"]
        assert "private destination" not in refused.text
        send.side_effect = TimeoutError()
        assert (await client.post(print_url, json=body)).status_code == 504
        state.config.zebra_host = ""
        assert (await client.post(print_url, json=body)).status_code == 503

    state.config.zebra_transport = "cups"
    state.config.zebra_cups_name = "mock-cups"
    lp.side_effect = FileNotFoundError("lp")
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png"):
        unavailable = await client.post(print_url, json=body)
        assert unavailable.status_code == 503
        assert "CUPS lp command unavailable" in unavailable.json()["detail"]
        lp.side_effect = None
        lp.return_value.returncode = 1
        lp.return_value.stderr = "mock CUPS rejected job"
        rejected = await client.post(print_url, json=body)
        assert rejected.status_code == 502
    saved = await Roast.get(roast.id)
    assert saved.bag_weights_grams == []
    assert saved.last_label_drop is None
