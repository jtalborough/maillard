"""WebSocket endpoint for real-time event streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from maillard.core.events import BaseEvent
from maillard.mcp.state import ApplicationState

_logger = logging.getLogger(__name__)


def _serialize_event(event: BaseEvent) -> str:
    """Serialize an event to JSON with a type discriminator."""
    data: dict[str, Any] = {"type": type(event).__name__}

    for key, value in event.__dict__.items():
        if key.startswith("_"):
            continue
        if isinstance(value, UUID):
            data[key] = str(value)
        elif isinstance(value, datetime):
            data[key] = value.isoformat()
        elif isinstance(value, frozenset):
            data[key] = list(value)
        elif hasattr(value, "value"):
            # Enum-like
            data[key] = value.value if not isinstance(value.value, int) else value.name
        elif isinstance(value, dict):
            # KaleidoState TypedDict — serialize directly
            data[key] = dict(value)
        else:
            data[key] = value

    return json.dumps(data)


async def websocket_endpoint(websocket: WebSocket) -> None:
    """Stream EventBus events to a connected browser client."""
    await websocket.accept()

    state: ApplicationState = websocket.app.state.maillard  # type: ignore[assignment]
    subscription = await state.event_bus.subscribe(BaseEvent)

    _logger.info("WebSocket client connected")

    try:
        while True:
            event = await subscription.get(timeout=30.0)
            if event is None:
                # Send keepalive ping
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            try:
                msg = _serialize_event(event)
                await websocket.send_text(msg)
            except Exception:
                _logger.debug("Failed to send event to WebSocket client", exc_info=True)
                break

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await subscription.close()
        _logger.info("WebSocket client disconnected")
