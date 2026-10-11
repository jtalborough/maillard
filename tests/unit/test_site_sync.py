"""Example Roastery v1 ingest, using in-memory Mongo and mock HTTP only."""

import json

import httpx
import pytest
from fastapi import FastAPI

from maillard.core.config import MaillardConfig, get_ingest_secret
from maillard.db.models import Coffee, Roast, RoastPlan, SyncOutbox
from maillard.mcp.state import ApplicationState
from maillard.sync.site import (
    SiteSyncClient,
    SiteSyncWorker,
    build_roast_payload,
    enqueue_roast_sync,
    process_outbox_once,
    sign_body,
    sync_roast_now,
)
from maillard.web.routes import create_router

pytestmark = pytest.mark.usefixtures("mongo_db")


def test_signature_vector():
    assert (
        sign_body(b'{"schema_version":1}', "test-secret")
        == "sha256=d7a36784d7854b79c962a6ffa2156953a387e990eaf600ad55a99fa2bb688bfc"
    )


@pytest.fixture
async def roast():
    r = Roast(
        status="completed",
        code="K7Q2MX",
        start_time="2026-10-01T16:00:00-04:00",
        end_time="2026-10-01T20:10:00Z",
        charge_temp=210,
        drop_temp=208,
        drop_time=600,
        fc_start_time=480,
        fc_start_temp=195,
        curve_data={
            "time_seconds": [0, 5],
            "bt": [210, 190],
            "et": [220],
            "bt_ror": [None, 12],
            "heat": [70, 70],
            "fan": [30, 30],
            "drum": [60, 60],
            "events": [
                {"type": "turning_point", "time": 80, "temp": 90},
                {"type": "turning_point", "time": 81, "temp": 91},
            ],
        },
    )
    await r.insert()
    return r


async def test_payload_shape(roast):
    coffee = Coffee(name="Éthiopia", origin="Ethiopia", processing="Washed")
    plan = RoastPlan(
        name="Plan",
        charge_temp=215,
        target_drop_temp=208,
        target_dtr=21,
        target_fc_seconds=480,
        target_total_seconds=600,
        roast_level="Medium-Light",
        brew_method="Filter",
        source="ai",
        ai_model="model",
        rationale="test",
        batch_size_grams=1200,
        phases=[
            {
                "name": "charge",
                "target_time_seconds": 0,
                "target_temp": 215,
                "heat_percent": 70,
                "fan_percent": 30,
                "drum_percent": 60,
                "target_ror": None,
                "notes": None,
            }
        ],
        planned_curve={"time_seconds": [0, 5], "bt": [215, 190]},
        locked_at="2026-10-01T16:00:00-04:00",
    )
    payload = build_roast_payload(roast, plan, coffee, version="test")
    assert set(payload) == {
        "schema_version",
        "sent_at",
        "source",
        "roast",
        "plan",
        "curve",
        "events",
    }
    assert set(payload["roast"]) == {
        "code",
        "maillard_id",
        "roast_name",
        "status",
        "control_mode",
        "published",
        "charged_at",
        "dropped_at",
        "total_time_seconds",
        "charge_temp_c",
        "drop_temp_c",
        "dtr_pct",
        "batch_size_grams",
        "green_weight_grams",
        "roasted_weight_grams",
        "bags_packed",
        "bag_size_grams",
        "drop",
        "weight_loss_pct",
        "roast_level",
        "notes",
        "bond_number",
        "coffee",
    }
    assert payload["roast"]["code"] == "K7Q2MX"
    assert payload["roast"]["charged_at"] == "2026-10-01T20:00:00Z"
    assert payload["roast"]["coffee"] == {
        "maillard_id": coffee.id,
        "name": "Éthiopia",
        "origin": "Ethiopia",
        "processing": "Washed",
    }
    assert set(payload["plan"]) == {
        "maillard_id",
        "name",
        "source",
        "ai_model",
        "rationale",
        "roast_level",
        "brew_method",
        "batch_size_grams",
        "charge_temp_c",
        "target_fc_seconds",
        "target_drop_temp_c",
        "target_total_seconds",
        "target_dtr_pct",
        "phases",
        "planned_curve",
        "locked_at",
    }
    assert payload["plan"]["charge_temp_c"] == 215
    assert payload["plan"]["target_drop_temp_c"] == 208
    assert payload["plan"]["target_dtr_pct"] == 21
    assert payload["plan"]["planned_curve"] == plan.planned_curve
    assert payload["plan"]["phases"] == plan.phases
    assert payload["plan"]["locked_at"].endswith("Z")
    assert payload["curve"]["et"] == []
    assert payload["curve"]["bt_ror"] == [None, 12]
    assert set(payload["curve"]) == {"time_seconds", "bt", "et", "bt_ror", "heat", "fan", "drum"}
    assert payload["events"] == [
        {"type": "charge", "time_seconds": 0, "temp_c": 210, "source": "operator"},
        {"type": "turning_point", "time_seconds": 80, "temp_c": 90, "source": "auto_detect"},
        {"type": "fc_start", "time_seconds": 480, "temp_c": 195, "source": "operator"},
        {"type": "drop", "time_seconds": 600, "temp_c": 208, "source": "operator"},
    ]
    empty = build_roast_payload(roast, None, None, version="test")
    assert empty["plan"] is None and empty["roast"]["coffee"] is None
    roast.status = "legacy"
    assert build_roast_payload(roast, None, None, version="test")["roast"]["status"] == "completed"
    roast.drop_time = None
    assert build_roast_payload(roast, None, None, version="test")["roast"]["status"] == "aborted"


def make_client(handler):
    return SiteSyncClient(
        MaillardConfig(ingest_secret="test-secret"),
        transport=httpx.MockTransport(handler),
        version="test",
    )


async def test_signed_bytes_headers_and_success(roast):
    def handler(request):
        payload = json.loads(request.content)
        assert (
            request.content
            == json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        )
        assert request.headers["X-Maillard-Signature"] == sign_body(request.content, "test-secret")
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Idempotency-Key"] == payload["roast"]["code"]
        assert int(request.headers["X-Maillard-Timestamp"]) > 0
        assert request.headers["User-Agent"] == "maillard/test"
        assert request.url.path == "/api/maillard/roasts"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "url": "https://roastery.example.com/r/K7Q2MX",
                "published": False,
                "warnings": ["coffee_not_matched"],
            },
        )

    client = make_client(handler)
    try:
        result = await sync_roast_now(roast.id, client)
        assert result["sync_status"] == "synced"
        r = await Roast.get(roast.id)
        item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id)
        assert item.status == r.site_sync_status == "synced"
        assert item.url == r.site_url == result["url"]
        assert r.site_synced_code == roast.code
        assert r.site_synced_at == item.synced_at
        assert r.site_published is False
        assert r.site_warnings == ["coffee_not_matched"]
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 413, 429, 500])
async def test_response_policy(roast, status):
    client = make_client(lambda _: httpx.Response(status, json={"error": "test"}))
    try:
        await enqueue_roast_sync(roast.id)
        await process_outbox_once(client, now=10**12)
        item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id)
        assert item.attempts == 1
        assert item.last_status_code == status
        assert item.status == ("pending" if status in (429, 500) else "failed")
        if status in (429, 500):
            assert item.next_attempt_at == 10**12 + 30
        if status == 401:
            assert "MAILLARD_INGEST_SECRET" in item.last_error
            assert (await process_outbox_once(client, now=10**13))["processed"] == 0
    finally:
        await client.aclose()


async def test_collision_regenerates_and_resigns(roast):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            409 if len(requests) == 1 else 200, json={"url": "https://site/r/new"}
        )

    client = make_client(handler)
    try:
        result = await sync_roast_now(roast.id, client)
        assert result["sync_status"] == "synced"
        assert result["code"] != roast.code
        assert requests[0]["sent_at"] != requests[1]["sent_at"]
        assert requests[1]["roast"]["code"] == result["code"]
    finally:
        await client.aclose()


async def test_enqueue_skips_and_resets(roast):
    active = Roast(status="in_progress")
    await active.insert()
    assert await enqueue_roast_sync(active.id) is None
    assert await SyncOutbox.find_one(SyncOutbox.roast_id == active.id) is None
    item = await enqueue_roast_sync(roast.id)
    item.status = "synced"
    item.attempts = 12
    await item.save()
    reset = await enqueue_roast_sync(roast.id)
    assert reset.id == item.id and reset.status == "pending" and reset.attempts == 0


async def test_only_due_items(roast):
    item = await enqueue_roast_sync(roast.id)
    item.next_attempt_at = 100
    await item.save()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"url": "https://site/r/test"})

    client = make_client(handler)
    try:
        assert (await process_outbox_once(client, now=99))["processed"] == 0
        assert not calls
        assert (await process_outbox_once(client, now=100))["processed"] == 1
        assert len(calls) == 1
    finally:
        await client.aclose()


async def test_api_routes(roast):
    client = make_client(lambda _: httpx.Response(200, json={"url": "https://site/r/test"}))
    app = FastAPI()
    app.state.maillard = ApplicationState(config=client.config)
    app.state.site_sync_client = client
    app.include_router(create_router())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            result = (await api.post(f"/api/roasts/{roast.id}/sync")).json()
            assert result["sync_status"] == "synced"
            assert set(result) == {"status", "sync_status", "url", "error", "code"}
            status = (await api.get("/api/sync/status")).json()
            assert status["configured"] and status["synced"] == 1
            assert status["pending"] == status["failed"] == 0
            state = (await api.get(f"/api/roasts/{roast.id}/sync")).json()
            assert state["outbox"]["status"] == state["sync_status"] == "synced"
    finally:
        await client.aclose()


async def test_worker_unconfigured_and_secret_file(tmp_path):
    config = MaillardConfig(ingest_secret=None, ingest_secret_file=None)
    client = SiteSyncClient(
        config, transport=httpx.MockTransport(lambda _: pytest.fail("unexpected HTTP"))
    )
    worker = SiteSyncWorker(client)
    await worker.start()
    assert worker._task is None
    await worker.stop()
    await client.aclose()
    secret = tmp_path / "secret"
    secret.write_text("file-secret\n")
    config.ingest_secret_file = secret
    assert get_ingest_secret(config) == "file-secret"
    secret.write_text("rotated-secret")
    assert get_ingest_secret(config) == "rotated-secret"


@pytest.mark.parametrize('failure', ['timeout', 'network'])
async def test_network_failure_backoff_and_limit(roast, failure):
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('mock timeout', request=request)
        raise httpx.ConnectError('mock connection error', request=request)

    client = make_client(handler)
    try:
        item = await enqueue_roast_sync(roast.id)
        now = item.next_attempt_at
        for attempt in range(1, 49):
            result = await process_outbox_once(client, now=now)
            assert result['processed'] == 1
            item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id)
            assert item.attempts == attempt
            assert item.last_status_code is None
            if attempt < 48:
                delay = [30, 60, 120, 300, 900][attempt - 1] if attempt <= 5 else 1800
                assert item.next_attempt_at == now + delay
                assert item.status == 'pending'
                now = item.next_attempt_at
            else:
                assert item.status == 'failed'
        assert (await process_outbox_once(client, now=now + 10000))['processed'] == 0
    finally:
        await client.aclose()


async def test_collision_limit(roast):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(409, json={'error': 'code_conflict'})
    client = make_client(handler)
    try:
        result = await sync_roast_now(roast.id, client)
        assert result['sync_status'] == 'failed'
        assert len(calls) == 4
        assert len({r.headers['Idempotency-Key'] for r in calls}) == 4
    finally:
        await client.aclose()


async def test_app_worker_lifecycle(roast):
    import asyncio

    from maillard.web.app import create_app

    client = make_client(lambda _: httpx.Response(200, json={'url': 'https://site/r/test'}))
    await enqueue_roast_sync(roast.id)
    app = create_app(ApplicationState(config=client.config))
    await app.state.site_sync_client.aclose()
    app.state.site_sync_client = client
    async with app.router.lifespan_context(app):
        for _ in range(20):
            await asyncio.sleep(0)
            item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast.id)
            if item.status == 'synced':
                break
        assert item.status == 'synced'
    assert client._http.is_closed
