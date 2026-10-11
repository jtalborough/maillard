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
                  roasted_weight_grams=1020, bags_packed=3)
    await roast.insert()
    url = f"/api/labels/print-readiness/{roast.id}"
    payload = {"roast_id": roast.id}
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
