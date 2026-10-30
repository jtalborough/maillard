"""Signed Example Roastery ingest and durable retry queue."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx
from pymongo.errors import DuplicateKeyError

from maillard.__version__ import VERSION
from maillard.core.config import MaillardConfig, get_config, get_ingest_secret
from maillard.db.models import Coffee, PresaleDrop, Roast, RoastPlan, SyncOutbox
from maillard.drop_label import canonical_series
from maillard.roasting import codes

_logger = logging.getLogger(__name__)
_BACKOFF = (30, 60, 120, 300, 900)


def _iso(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return (
        dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        if dt.tzinfo is None
        else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )


def build_roast_payload(
    roast: Roast, plan: RoastPlan | None, coffee: Coffee | None, *, version: str, drop: PresaleDrop | None = None
) -> dict[str, Any]:
    """Build exactly the v1 ingest shape from persisted snapshots."""
    mapping = {
        "maillard_id": "id",
        "charged_at": "start_time",
        "dropped_at": "end_time",
        "charge_temp_c": "charge_temp",
        "drop_temp_c": "drop_temp",
        "dtr_pct": "dtr",
        "weight_loss_pct": "weight_loss",
    }
    fields = (
        "code",
        "roast_name",
        "control_mode",
        "published",
        "total_time_seconds",
        "batch_size_grams",
        "green_weight_grams",
        "roasted_weight_grams",
        "bags_packed",
        "bag_size_grams",
        "notes",
        "bond_number",
    )
    data = {key: getattr(roast, key) for key in fields}
    if canonical_series(roast.drop_series):
        data["bond_number"] = None
    data.update({key: getattr(roast, attr) for key, attr in mapping.items()})
    data["charged_at"] = _iso(data["charged_at"])
    data["dropped_at"] = _iso(data["dropped_at"])
    data["status"] = (
        roast.status
        if roast.status in ("completed", "aborted")
        else ("completed" if roast.drop_time is not None else "aborted")
    )
    data["drop"] = {"maillard_id": drop.id, "drop_number": drop.drop_number} if drop else None
    data["roast_level"] = plan.roast_level if plan else None
    data["coffee"] = (
        {
            "maillard_id": coffee.id,
            "name": coffee.name,
            "origin": coffee.origin,
            "processing": coffee.processing,
        }
        if coffee
        else None
    )
    plan_data = None
    if plan:
        plan_data = {
            key: getattr(plan, key)
            for key in (
                "name",
                "source",
                "ai_model",
                "rationale",
                "roast_level",
                "brew_method",
                "batch_size_grams",
                "target_fc_seconds",
                "target_total_seconds",
                "phases",
                "planned_curve",
            )
        }
        plan_data.update(
            maillard_id=plan.id,
            charge_temp_c=plan.charge_temp,
            target_drop_temp_c=plan.target_drop_temp,
            target_dtr_pct=plan.target_dtr,
            locked_at=_iso(plan.locked_at),
        )
        plan_data["phases"] = sorted(plan.phases, key=lambda p: p.get("target_time_seconds", 0))
    raw = roast.curve_data or {}
    times = raw.get("time_seconds") or []
    curve = {"time_seconds": times}
    for key in ("bt", "et", "bt_ror", "heat", "fan", "drum"):
        values = raw.get(key) or []
        curve[key] = values if len(values) == len(times) else []
    events = {}
    valid = (
        "charge",
        "turning_point",
        "dry_end",
        "fc_start",
        "fc_end",
        "sc_start",
        "drop",
        "emergency_stop",
    )
    for event in raw.get("events", []):
        kind = event.get("type")
        if kind in valid and kind not in events:
            events[kind] = {
                "type": kind,
                "time_seconds": event["time"],
                "temp_c": event.get("temp"),
                "source": "auto_detect" if kind == "turning_point" else "operator",
            }
    for kind in valid[:-1]:
        t = (
            (0 if roast.start_time or roast.charge_temp is not None else None)
            if kind == "charge"
            else getattr(roast, kind + "_time")
        )
        if kind not in events and t is not None:
            events[kind] = {
                "type": kind,
                "time_seconds": t,
                "temp_c": roast.charge_temp if kind == "charge" else getattr(roast, kind + "_temp"),
                "source": "auto_detect" if kind == "turning_point" else "operator",
            }
    return {
        "schema_version": 1,
        "sent_at": _iso(datetime.now(UTC)),
        "source": {"app": "maillard", "version": version},
        "roast": data,
        "plan": plan_data,
        "curve": curve,
        "events": sorted(events.values(), key=lambda e: e["time_seconds"]),
    }


def sign_body(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SiteSyncClient:
    def __init__(
        self,
        config: MaillardConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        version: str = VERSION,
    ) -> None:
        self.config = config or get_config()
        self.version = version
        self._http = httpx.AsyncClient(timeout=15, transport=transport)
        self.lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(get_ingest_secret(self.config))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, roast: Roast) -> httpx.Response:
        await codes.ensure_roast_code(roast)
        plan = await RoastPlan.get(roast.plan_id) if roast.plan_id else None
        coffee = await Coffee.get(roast.coffee_id) if roast.coffee_id else None
        drop = await PresaleDrop.get(roast.drop_id) if roast.drop_id else None
        payload = build_roast_payload(roast, plan, coffee, version=self.version, drop=drop)
        return await self._post("/api/maillard/roasts", payload, key=roast.code)

    async def _post(self, path: str, payload: dict, *, key: str | None = None) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        secret = get_ingest_secret(self.config)
        if not secret:
            raise ValueError("Check MAILLARD_INGEST_SECRET")
        headers = {
            "Content-Type": "application/json",
            "X-Maillard-Signature": sign_body(body, secret),
            "X-Maillard-Timestamp": str(int(time.time())),
            "User-Agent": "maillard/" + self.version,
        }
        if key:
            headers["Idempotency-Key"] = key
        return await self._http.post(self.config.site_url.rstrip("/") + path,
                                     content=body, headers=headers)

    async def push_drop(self, drop: PresaleDrop) -> dict:
        async with self.lock:
            current = await PresaleDrop.get(drop.id)
            if current is None:
                raise ValueError("Drop not found")
            if current.status in ("roasted", "cancelled", "closed") and current.status != drop.status:
                raise ValueError("Drop lifecycle changed; reload before syncing")
            return await self._push_drop(drop)

    async def _push_drop(self, drop: PresaleDrop) -> dict:
        from maillard.inventory.service import (
            apply_drop_status,
            mark_drop_sync_attempted,
        )
        if drop.status == "roasted":
            raise ValueError("Roasted drops sync through roast ingest and drop status")
        fields = ("drop_number", "status", "capacity_bags", "bag_size_grams", "expected_yield",
                  "green_grams_per_bag")
        payload = {"schema_version": 1, "sent_at": _iso(datetime.now(UTC)),
                   "drop": {key: getattr(drop, key) for key in fields}}
        # Store column is an integer number of grams.
        payload["drop"]["bag_size_grams"] = int(round(drop.bag_size_grams))
        payload["drop"].update(maillard_id=drop.id, coffee_maillard_id=drop.coffee_id,
                               green_reserved_grams=drop.capacity_bags * drop.green_grams_per_bag)
        # Capacity proposals advertise the reservation after local reconciliation.
        await mark_drop_sync_attempted(drop.id)
        response = await self._post("/api/maillard/drops", payload)
        response.raise_for_status()
        data = response.json()
        await apply_drop_status(data["drop"])
        return data

    async def fetch_drop_status(self, ids: list[str]) -> dict:
        async with self.lock:
            return await self._fetch_drop_status(ids)

    async def _fetch_drop_status(self, ids: list[str]) -> dict:
        from maillard.inventory.service import apply_drop_status
        response = await self._post("/api/maillard/drops/status",
                                   {"schema_version": 1, "sent_at": _iso(datetime.now(UTC)),
                                    "maillard_ids": ids})
        response.raise_for_status()
        data = response.json()
        for drop in data["drops"]:
            await apply_drop_status(drop)
        return data


async def enqueue_roast_sync(roast_id: str) -> SyncOutbox | None:
    roast = await Roast.get(roast_id)
    if roast is None or roast.status not in ("completed", "aborted"):
        return None
    code = await codes.ensure_roast_code(roast)
    item = await SyncOutbox.find_one(SyncOutbox.roast_id == roast_id)
    if item is None:
        item = SyncOutbox(roast_id=roast_id, code=code, next_attempt_at=time.time())
        try:
            await item.insert()
        except DuplicateKeyError:
            return await enqueue_roast_sync(roast_id)
    elif item.status != "pending":
        item.status = "pending"
        item.attempts = 0
        item.next_attempt_at = time.time()
        item.last_error = None
        item.last_status_code = None
        item.code = code
        item.updated_at = _iso(datetime.now(UTC))
        await item.save()
    await roast.set({"site_sync_status": "pending"})
    return item


def _result(item: SyncOutbox, roast: Roast) -> dict[str, Any]:
    return {
        "status": "ok",
        "sync_status": item.status,
        "url": item.url,
        "error": item.last_error,
        "code": roast.code,
    }


async def _attempt(item: SyncOutbox, client: SiteSyncClient, now: float) -> dict[str, Any]:
    roast = await Roast.get(item.roast_id)
    if roast is None or roast.status not in ("completed", "aborted"):
        item.status = "failed"
        item.last_error = "Roast missing or unfinished"
        await item.save()
        return {"status": "error", "sync_status": "failed", "error": item.last_error}
    for collision in range(4):
        item.attempts += 1
        item.last_status_code = None
        try:
            response = await client.send(roast)
            item.last_status_code = response.status_code
            try:
                data = response.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            item.last_error = str(data.get("error") or f"HTTP {response.status_code}")
            if response.status_code == 200:
                item.status = "synced"
                item.url = data.get("url")
                item.synced_at = _iso(datetime.now(UTC))
                item.last_error = None
                warnings = data.get("warnings") or []
                published = data.get("published")
                await roast.set(
                    {
                        "site_url": item.url,
                        "site_synced_at": item.synced_at,
                        "site_synced_code": roast.code,
                        # Effective visibility on the site (published AND
                        # matched coffee AND completed). /r/<code> 404s
                        # unless this is true.
                        "site_published": published if isinstance(published, bool) else None,
                        "site_warnings": [str(w) for w in warnings]
                        if isinstance(warnings, list)
                        else [],
                    }
                )
                break
            if response.status_code == 409 and collision < 3 and item.attempts < 48:
                await codes.regenerate_roast_code(roast)
                continue
            if response.status_code == 401:
                item.last_error += "; check MAILLARD_INGEST_SECRET"
            if response.status_code != 429 and response.status_code < 500:
                item.status = "failed"
                break
        except httpx.RequestError as exc:
            item.last_error = type(exc).__name__
        if item.attempts >= 48:
            item.status = "failed"
        else:
            item.next_attempt_at = now + (
                _BACKOFF[item.attempts - 1] if item.attempts <= 5 else 1800
            )
        break
    item.code = roast.code
    item.updated_at = _iso(datetime.now(UTC))
    await item.save()
    await roast.set({"site_sync_status": item.status})
    return _result(item, roast)


async def sync_roast_now(roast_id: str, client: SiteSyncClient) -> dict[str, Any]:
    async with client.lock:
        item = await enqueue_roast_sync(roast_id)
        if item is None:
            return {
                "status": "error",
                "sync_status": None,
                "url": None,
                "error": "Roast missing or unfinished",
                "code": None,
            }
        roast = await Roast.get(roast_id)
        if not client.is_configured:
            result = _result(item, roast)
            result["error"] = "Site sync not configured"
            return result
        return await _attempt(item, client, time.time())


async def process_outbox_once(client: SiteSyncClient, now: float | None = None) -> dict[str, Any]:
    results = []
    if client.is_configured:
        async with client.lock:
            now = time.time() if now is None else now
            items = await SyncOutbox.find(
                SyncOutbox.status == "pending", SyncOutbox.next_attempt_at <= now
            ).to_list()
            for item in items:
                results.append(await _attempt(item, client, now))
    return {"processed": len(results), "results": results}


class SiteSyncWorker:
    def __init__(self, client: SiteSyncClient) -> None:
        self.client = client
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.client.is_configured and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop(), name="site-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await process_outbox_once(self.client)
            except Exception:
                _logger.exception("Site sync worker failed")
            await asyncio.sleep(self.client.config.site_sync_interval_seconds)
