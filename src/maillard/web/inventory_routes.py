"""Inventory REST routes, mounted under the dashboard's existing /api router."""

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from maillard.db.models import GreenLot, InventoryMove, PresaleDrop
from maillard.inventory import service
from maillard.sync.site import SiteSyncClient

router = APIRouter()


class Receipt(BaseModel):
    coffee_id: str
    grams_in: float = Field(ge=0, allow_inf_nan=False)
    supplier: str | None = None
    cost_per_kg: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    received_at: str | None = None
    notes: str | None = None
    idempotency_key: str | None = None


class Adjustment(BaseModel):
    grams: float = Field(allow_inf_nan=False)
    note: str = Field(min_length=1)
    force: bool = False
    idempotency_key: str | None = None


class DropOpen(BaseModel):
    coffee_id: str
    capacity_bags: int = Field(ge=0)
    bag_size_grams: float = Field(default=340, gt=0, allow_inf_nan=False)
    expected_yield: float = Field(default=0.85, gt=0, le=1, allow_inf_nan=False)
    idempotency_key: str | None = None


class DropResize(BaseModel):
    capacity_bags: int = Field(ge=0)


def site_client(request: Request) -> SiteSyncClient:
    client = getattr(request.app.state, "site_sync_client", None)
    if client is None:
        client = SiteSyncClient(request.app.state.maillard.config)
        request.app.state.site_sync_client = client
    return client


async def checked(call):
    try:
        return await call
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except ValueError:
            detail = "Store rejected drop update"
        raise HTTPException(exc.response.status_code, detail) from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "Store unavailable; green reservation retained") from exc


@router.get("/inventory/lots")
async def lots(coffee_id: str | None = None):
    query = GreenLot.find({"coffee_id": coffee_id}) if coffee_id else GreenLot.find_all()
    return [lot.model_dump() for lot in await query.sort("received_at", "_id").to_list()]


@router.post("/inventory/lots")
async def receive(body: Receipt):
    data = body.model_dump()
    data["grams"] = data.pop("grams_in")
    return (await checked(service.receive_lot(**data))).model_dump()


@router.post("/inventory/lots/{lot_id}/adjust")
async def adjust(lot_id: str, body: Adjustment):
    if await GreenLot.get(lot_id) is None:
        raise HTTPException(404, "Lot not found")
    return (await checked(service.adjust(lot_id, **body.model_dump()))).model_dump()


@router.get("/inventory/moves")
async def moves(coffee_id: str | None = None):
    query = InventoryMove.find({"coffee_id": coffee_id}) if coffee_id else InventoryMove.find_all()
    return [move.model_dump() for move in await query.sort("-at").to_list()]


@router.get("/inventory/summary")
async def summary():
    return await service.inventory_summary()


@router.get("/drops")
async def drops():
    return [
        await service.drop_summary(drop)
        for drop in await PresaleDrop.find_all().sort("-created_at").to_list()
    ]


@router.post("/drops")
async def open_drop(body: DropOpen):
    return await service.drop_summary(await checked(service.open_drop(**body.model_dump())))


@router.post("/drops/{drop_id}/resize")
async def resize_drop(request: Request, drop_id: str, body: DropResize):
    drop = await PresaleDrop.get(drop_id)
    if drop is None:
        raise HTTPException(404, "Drop not found")
    if drop.status != "open":
        raise HTTPException(409, "Only open drops can be resized")
    client = site_client(request)
    if drop.drop_number is not None or drop.site_sync_attempted:
        if not client.is_configured:
            raise HTTPException(409, "Configure store sync before resizing a synced drop")
        await checked(client.fetch_drop_status([drop_id]))
        drop = await PresaleDrop.get(drop_id)
        if body.capacity_bags < drop.bags_sold + drop.bags_held:
            raise HTTPException(409, "Capacity below sold bags plus active holds")
        if body.capacity_bags < drop.capacity_bags:
            # Store locks capacity against concurrent sales before we release any green.
            proposal = drop.model_copy(update={"capacity_bags": body.capacity_bags})
            await checked(client.push_drop(proposal))
    result = await checked(service.resize_drop(drop_id, body.capacity_bags))
    if (
        drop.drop_number is not None or drop.site_sync_attempted
    ) and body.capacity_bags >= drop.capacity_bags:
        # Reserve increases first; a rejected push leaves extra green reserved safely.
        await checked(client.push_drop(result))
    return await service.drop_summary(result)


async def finish_drop(request: Request, drop_id: str, action: Literal["close", "cancel"]):
    drop = await PresaleDrop.get(drop_id)
    if drop is None:
        raise HTTPException(404, "Drop not found")
    target_status = "closed" if action == "close" else "cancelled"
    if drop.status == target_status:
        return await service.drop_summary(drop)
    if drop.status in ("cancelled", "roasted"):
        raise HTTPException(409, "Drop already cancelled or roasted")
    client = site_client(request)
    # If a drop has reached the store, never release green while offline.
    if (drop.drop_number is not None or drop.site_sync_attempted) and not client.is_configured:
        raise HTTPException(409, "Configure store sync before closing a synced drop")
    if client.is_configured:
        await checked(client.fetch_drop_status([drop_id]))
        drop = await PresaleDrop.get(drop_id)
    if action == "cancel" and (drop.bags_sold or drop.bags_held):
        raise HTTPException(409, "Cannot cancel a drop with sold bags or active holds")
    capacity = drop.bags_sold if action == "close" else 0
    if drop.bags_held:
        raise HTTPException(409, "Active checkout holds; retry after they settle")
    if client.is_configured:
        proposal = drop.model_copy(update={"status": target_status, "capacity_bags": capacity})
        await checked(client.push_drop(proposal))
    result = await checked(
        service.close_drop(drop_id) if action == "close" else service.cancel_drop(drop_id)
    )
    return await service.drop_summary(result)


@router.post("/drops/{drop_id}/close")
async def close_drop(request: Request, drop_id: str):
    return await finish_drop(request, drop_id, "close")


@router.post("/drops/{drop_id}/cancel")
async def cancel_drop(request: Request, drop_id: str):
    return await finish_drop(request, drop_id, "cancel")


@router.post("/drops/{drop_id}/sync")
async def sync_drop(request: Request, drop_id: str):
    drop = await PresaleDrop.get(drop_id)
    if drop is None:
        raise HTTPException(404, "Drop not found")
    client = site_client(request)
    if not client.is_configured:
        raise HTTPException(409, "Store sync not configured")
    if drop.status == "roasted":
        await checked(client.fetch_drop_status([drop_id]))
    else:
        await checked(client.push_drop(drop))
        await checked(client.fetch_drop_status([drop_id]))
    return await service.drop_summary(await PresaleDrop.get(drop_id))
