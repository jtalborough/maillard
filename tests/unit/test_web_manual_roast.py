"""Simulator-backed web dashboard roast flow (manual-first).

Charges a roast through the real FastAPI routes against the SimulatorDriver
and verifies that a manual roast is not emergency-stopped by the watchdog.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from maillard.core.config import MaillardConfig
from maillard.driver.simulator import SimulatorDriver
from maillard.mcp.state import ApplicationState


@pytest.fixture
async def sim_app(mongo_db):  # noqa: ARG001 - fixture initializes Beanie
    from maillard.web.routes import create_router

    sim = SimulatorDriver()
    await sim.connect("simulator")
    state = ApplicationState(config=MaillardConfig(poll_interval=0.1))
    state.driver = sim

    app = FastAPI()
    app.state.maillard = state
    app.state.ai_agent = None
    app.include_router(create_router())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, state, app, sim

    await state.halt_heat_sources()
    for task in (state._monitor, state._safety_monitor):
        if task is not None:
            await task.stop()
    await sim.disconnect()


@pytest.mark.asyncio
async def test_manual_roast_not_estopped_by_watchdog(sim_app) -> None:
    client, state, _app, sim = sim_app

    resp = await client.post("/api/charge", json={"batch_size_grams": 1200})
    data = resp.json()
    assert data["status"] == "charged", data

    monitor = state._safety_monitor
    assert monitor is not None
    assert monitor.watchdog_enforced is False

    # Pretend no AI/MCP heartbeat for two minutes.
    monitor._last_tool_call_time = time.time() - 120
    await monitor._check_watchdog()
    await asyncio.sleep(0)

    assert state.is_roast_active
    assert sim.state["FC"] != 100 or sim.state["HP"] != 0  # no e-stop applied
    assert "watchdog_emergency" not in monitor.get_status()["active_alerts"]

    from maillard.db.models import Roast

    roast = await Roast.get(data["roast_id"])
    assert roast is not None
    assert roast.control_mode == "manual"

    drop = (await client.post("/api/drop")).json()
    assert drop["status"] == "dropped", drop


@pytest.mark.asyncio
async def test_ai_auto_refused_when_flag_off(sim_app) -> None:
    client, state, app, _sim = sim_app
    from maillard.ai.agent import RoastAIAgent

    agent = RoastAIAgent(state, MagicMock())
    app.state.ai_agent = agent

    resp = (await client.post("/api/ai/toggle", json={"mode": "auto"})).json()
    assert resp["status"] == "error"
    assert "MAILLARD_AI_AUTO_CONTROL_ENABLED" in resp["error"]

    status = (await client.get("/api/ai/status")).json()
    assert status["auto_control_enabled"] is False
    assert status["mode"] == "off"


@pytest.mark.asyncio
async def test_ai_auto_enforces_watchdog_when_enabled(sim_app) -> None:
    client, state, app, _sim = sim_app
    from maillard.ai.agent import RoastAIAgent

    state.config = MaillardConfig(poll_interval=0.1, ai_auto_control_enabled=True)
    agent = RoastAIAgent(state, MagicMock())
    app.state.ai_agent = agent

    assert (await client.post("/api/charge", json={})).json()["status"] == "charged"
    assert state._safety_monitor.watchdog_enforced is False

    resp = (await client.post("/api/ai/toggle", json={"mode": "auto"})).json()
    assert resp == {"status": "ok", "mode": "auto"}
    assert state._safety_monitor.watchdog_enforced is True

    from maillard.db.models import Roast

    roast = await Roast.get(state.roast_context.roast_id)
    assert roast.control_mode == "auto"

    await agent.set_mode(agent.mode.__class__.OFF)
    await client.post("/api/drop")


@pytest.mark.asyncio
async def test_charge_with_profile_stores_locked_plan(sim_app) -> None:
    client, state, _app, _sim = sim_app
    from maillard.ai.generate import compute_heuristic_profile
    from maillard.db.models import Profile, Roast, RoastPlan

    gp = compute_heuristic_profile(None, roast_level="Medium-Light", batch_size_grams=1200)
    profile = Profile(
        name=gp.name,
        phases=gp.phases,
        charge_temp=gp.charge_temp,
        target_dtr=gp.target_dtr,
        batch_size_grams=1200,
        plan_source="heuristic",
        warnings=["AI plan rejected: invalid targets; using calibrated heuristic"],
    )
    await profile.insert()

    preview = (
        await client.get(f"/api/profiles/{profile.id}/planned-curve?batch_size_grams=1200")
    ).json()
    assert preview["status"] == "ok"
    assert preview["plan"]["locked_at"] is None
    assert profile.warnings[0] in preview["plan"]["warnings"]
    assert any("extrapolated" in w for w in preview["plan"]["warnings"])

    data = (
        await client.post(
            "/api/charge", json={"profile_id": profile.id, "batch_size_grams": 1200}
        )
    ).json()
    assert data["status"] == "charged"
    plan = data["plan"]
    assert plan["source"] == "heuristic"
    assert plan["batch_size_grams"] == 1200
    assert plan["planned_curve"]["time_seconds"][-1] == plan["target_total_seconds"]

    roast = await Roast.get(data["roast_id"])
    assert roast.plan_id == plan["id"]
    stored = await RoastPlan.get(plan["id"])
    assert stored.roast_id == roast.id
    assert stored.locked_at is not None
    assert profile.warnings[0] in stored.warnings

    active = (await client.get("/api/roast/plan")).json()
    assert active["plan"]["id"] == plan["id"]

    await client.post("/api/drop")
    after = (await client.get(f"/api/roasts/{roast.id}/plan")).json()
    assert after["plan"]["id"] == plan["id"]


@pytest.mark.asyncio
async def test_generate_profile_defaults_to_1200g(sim_app) -> None:
    client, _state, _app, _sim = sim_app
    from maillard.ai.calibration import get_calibration
    from maillard.db.models import Coffee

    coffee = Coffee(name="Ethiopia Uraga", origin="Ethiopia", processing="Washed")
    await coffee.insert()
    data = (
        await client.post("/api/ai/generate-profile", json={"coffee_id": coffee.id})
    ).json()
    assert data["status"] == "ok", data
    p = data["profile"]
    assert p["batch_size_grams"] == 1200
    assert p["charge_temp"] == get_calibration(p["batch_size_grams"])["charge_temp_c"]
    assert p["planned_curve"]["bt"]
    assert any("extrapolated" in w for w in p["warnings"])


@pytest.mark.asyncio
async def test_charge_assigns_code_and_weights_flow(sim_app) -> None:
    client, _state, _app, _sim = sim_app
    from maillard.utils.naming import is_valid_roast_code

    data = (await client.post("/api/charge", json={"batch_size_grams": 1200})).json()
    assert is_valid_roast_code(data["code"])
    await client.post("/api/drop")
    rid = data["roast_id"]

    detail = (await client.get(f"/api/roasts/{rid}")).json()
    assert detail["code"] == data["code"]
    assert detail["green_weight_grams"] == 1200
    assert detail["control_mode"] == "manual"

    bad = (await client.post(f"/api/roasts/{rid}/weights", json={"roasted_weight_grams": 1300})).json()
    assert bad["status"] == "error"

    ok = (await client.post(f"/api/roasts/{rid}/weights", json={"roasted_weight_grams": 1020})).json()
    assert ok == {
        "status": "ok",
        "green_weight_grams": 1200,
        "roasted_weight_grams": 1020,
        "weight_loss": 15.0,
        "bags_packed": 3, "bag_size_grams": 340, "drop_id": None,
        "inventory": {"deducted": False, "warning": "Roast unfinished or coffee unknown"},
    }


@pytest.mark.asyncio
async def test_ensure_code_backfills_old_roast(sim_app) -> None:
    client, _state, _app, _sim = sim_app
    from maillard.db.models import Roast

    old = Roast(status="completed")
    await old.insert()
    r = (await client.post(f"/api/roasts/{old.id}/ensure-code")).json()
    assert r["status"] == "ok" and len(r["code"]) == 6
    r2 = (await client.post(f"/api/roasts/{old.id}/ensure-code")).json()
    assert r2["code"] == r["code"]


@pytest.mark.asyncio
async def test_drop_enqueues_site_sync(sim_app):
    from maillard.db.models import SyncOutbox
    from maillard.web.routes import _sync_tasks

    client, _state, _app, _sim = sim_app
    charge = (await client.post('/api/charge', json={'batch_size_grams': 1200})).json()
    assert charge['status'] == 'charged'
    assert (await client.post('/api/drop')).json()['status'] == 'dropped'
    await asyncio.gather(*_sync_tasks)
    item = await SyncOutbox.find_one(SyncOutbox.roast_id == charge['roast_id'])
    assert item is not None and item.status == 'pending'


@pytest.mark.asyncio
async def test_emergency_stop_enqueues_aborted_roast(sim_app):
    from maillard.db.models import Roast, SyncOutbox
    from maillard.web.routes import _sync_tasks

    client, state, _app, sim = sim_app
    charge = (await client.post('/api/charge', json={})).json()
    result = (await client.post('/api/emergency-stop')).json()
    assert result['status'] == 'emergency_stopped'
    assert not state.is_roast_active and sim.state['HP'] == 0
    await asyncio.gather(*_sync_tasks)
    roast = await Roast.get(charge['roast_id'])
    item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id)
    assert roast.status == 'aborted'
    assert item.status == 'pending'


@pytest.mark.asyncio
async def test_drop_persists_all_session_phases(sim_app):
    from maillard.db.models import Roast
    from maillard.roasting.session import RoastSession

    client, state, _app, _sim = sim_app
    roast = Roast(batch_size_grams=450)
    await roast.insert()
    session = RoastSession.start(state.event_bus, charge_temp=200, roast_id=roast.id)
    phases = [("turning_point", 40, 85), ("dry_end", 240, 150),
              ("fc_start", 400, 182), ("fc_end", 450, 190), ("sc_start", 480, 198)]
    for phase, seconds, temp in phases:
        session.mark_event(phase, temp=temp, time_seconds=seconds)
    state.roast_session = session
    state.roast_context.roast_id = roast.id
    state.roast_context.is_active = True
    result = (await client.post("/api/drop")).json()
    assert result["status"] == "dropped"
    assert result["cooling"]["enabled"] is True
    saved = await Roast.get(roast.id)
    for phase, seconds, temp in phases:
        assert getattr(saved, f"{phase}_time") == seconds
        assert getattr(saved, f"{phase}_temp") == temp
    assert saved.status == "completed"
    assert saved.drop_time is not None


@pytest.mark.asyncio
async def test_generated_warnings_survive_profile_save(sim_app, monkeypatch):
    from maillard.ai.generate import compute_heuristic_profile
    from maillard.db.models import Profile

    client, _state, _app, _sim = sim_app
    generated = compute_heuristic_profile(None, batch_size_grams=450)
    generated.warnings = ["Charge temperature mismatch repaired from charge phase"]

    async def generate(**_kwargs):
        return generated

    monkeypatch.setattr("maillard.ai.generate.generate_profile", generate)
    response = (await client.post("/api/ai/generate-profile", json={"coffee_id": "test"})).json()
    assert response["status"] == "ok"
    payload = response["profile"]
    assert generated.warnings[0] in payload["warnings"]
    saved = (await client.post("/api/profiles", json={
        "name": payload["name"], "phases": payload["phases"],
        "charge_temp": payload["charge_temp"], "target_dtr": payload["target_dtr"],
        "batch_size_grams": 450, "warnings": payload["warnings"],
    })).json()
    stored = await Profile.get(saved["id"])
    assert generated.warnings[0] in stored.warnings
    fetched = (await client.get(f"/api/profiles/{stored.id}")).json()
    assert generated.warnings[0] in fetched["warnings"]
    preview = (await client.get(f"/api/profiles/{stored.id}/planned-curve")).json()
    assert generated.warnings[0] in preview["plan"]["warnings"]


@pytest.mark.asyncio
async def test_preheat_is_monitored_and_charge_replaces_monitor(sim_app):
    client, state, _app, _sim = sim_app
    state.config.preheat_timeout_seconds = 30
    result = (await client.post('/api/preheat', json={'target_temp': 200})).json()
    assert result['status'] == 'preheating'
    preheat_monitor = state._monitor
    assert preheat_monitor.is_running
    assert state._heating_manager._preheat_deadline is not None
    charge = (await client.post('/api/charge', json={})).json()
    assert charge['status'] == 'charged'
    assert not preheat_monitor.is_running
    assert preheat_monitor._freshness_task is None
    assert state._monitor.is_running
    assert state._heating_manager._preheat_deadline is None
    await client.post('/api/drop')


@pytest.mark.asyncio
async def test_configured_preheat_timeout_through_web(sim_app):
    from maillard.core.events import SafetyAlertEvent

    client, state, _app, sim = sim_app
    state.config.preheat_timeout_seconds = 0.01
    events = []
    state.event_bus.on(SafetyAlertEvent, events.append)
    assert (await client.post('/api/preheat', json={'target_temp': 200})).json()['status'] == 'preheating'
    # Accelerate the background check without waiting its default 1s interval.
    state._heating_manager._send_interval = 0.001
    for _ in range(150):
        if not state._heating_manager.is_running:
            break
        await asyncio.sleep(0.01)
    assert not state._heating_manager.is_running
    assert sim.state['HP'] == 0 and sim.state['HS'] == 0
    assert any(e.condition == 'preheat_timeout' and not e.cleared for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['drop', 'emergency-stop'])
async def test_connection_events_persisted(sim_app, endpoint):
    from maillard.db.converters import roast_to_response_with_curve
    from maillard.db.models import Roast

    client, state, _app, sim = sim_app
    charged = (await client.post('/api/charge', json={})).json()
    assert charged['status'] == 'charged'
    await sim.disconnect()
    await state._monitor._poll_once()
    await sim.connect('simulator')
    await state._monitor._poll_once()
    result = (await client.post(f'/api/{endpoint}')).json()
    assert result['status'] in {'dropped', 'emergency_stopped'}
    roast = await Roast.get(charged['roast_id'])
    assert roast is not None
    assert [e['type'] for e in roast.connection_events] == ['closed', 'connected']
    assert all(e['elapsed'] is not None for e in roast.connection_events)
    assert roast.curve_data['gaps'][0]['reason'] == 'disconnected'
    assert roast.curve_data['connection_summary']['disconnects'] == 1
    assert roast_to_response_with_curve(roast).connection_events == roast.connection_events
