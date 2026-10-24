"""Copied label text uses the brand the label renderer prints."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from maillard.core.config import MaillardConfig
from maillard.web.routes import create_router

_HISTORY_JS = (
    Path(__file__).resolve().parents[2] / "src/maillard/web/static/js/history.js"
)


def _request(brand: str) -> Request:
    app = FastAPI()
    app.state.maillard = SimpleNamespace(
        config=MaillardConfig(_env_file=None, label_brand_name=brand),
    )
    return Request({"type": "http", "app": app})


def _list_label_styles():
    router = create_router()
    return next(route.endpoint for route in router.routes if route.path == "/api/label-styles")


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", ["", "Test Roasters"])
async def test_label_styles_expose_renderer_brand(brand: str) -> None:
    styles = await _list_label_styles()(_request(brand))
    by_id = {style["id"]: style["brand_name"] for style in styles}
    assert "drop" in by_id
    assert "classic" in by_id
    if brand:
        assert set(by_id.values()) == {brand}
    else:
        assert by_id["drop"] == "Example Roastery"
        assert by_id["classic"] == "Example Roastery"


def test_copy_label_text_reads_style_brand() -> None:
    source = _HISTORY_JS.read_text()
    assert "parts.push('Example Roastery')" not in source
    assert "selected.brand_name" in source
    assert "|| 'Example Roastery'" in source
