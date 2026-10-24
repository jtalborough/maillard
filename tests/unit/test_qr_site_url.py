from unittest.mock import patch

import httpx
import qrcode
from fastapi import FastAPI

from maillard.core.config import MaillardConfig
from maillard.mcp.state import ApplicationState
from maillard.web.routes import create_router


async def test_coffee_qr_uses_configured_site_url():
    app = FastAPI()
    app.state.maillard = ApplicationState(
        config=MaillardConfig(site_url="https://store.invalid/", ingest_secret_file=None)
    )
    app.state.ai_agent = None
    app.include_router(create_router())
    seen = []
    original = qrcode.QRCode.add_data

    def capture(self, data, *args, **kwargs):
        seen.append(data)
        return original(self, data, *args, **kwargs)

    with patch.object(qrcode.QRCode, "add_data", capture):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/qr/house-blend")
    assert response.status_code == 200
    assert seen == ["https://store.invalid/coffees/house-blend"]
