"""Label copy text and the roast-code tooltip follow configured label settings."""

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


def _request(brand: str = "", site_url: str | None = None) -> Request:
    config = {"_env_file": None, "label_brand_name": brand}
    if site_url is not None:
        config["site_url"] = site_url
    app = FastAPI()
    app.state.maillard = SimpleNamespace(config=MaillardConfig(**config))
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("site_url", "host"),
    [
        (None, "roastery.example.com"),
        ("https://cups.example.com/", "cups.example.com"),
        ("https://www.cups.example.com/roasts", "cups.example.com"),
    ],
)
async def test_label_styles_expose_site_host(site_url: str | None, host: str, monkeypatch) -> None:
    monkeypatch.delenv("MAILLARD_SITE_URL", raising=False)
    styles = await _list_label_styles()(_request(site_url=site_url))
    assert styles
    assert {style["site_host"] for style in styles} == {host}


def test_roast_code_tooltip_uses_site_host() -> None:
    source = _HISTORY_JS.read_text()
    assert "roastery.example.com/r/" not in source
    assert "style.site_host" in source
    assert "|| 'roastery.example.com'" in source


def test_copy_label_text_reads_style_brand() -> None:
    source = _HISTORY_JS.read_text()
    assert "parts.push('Example Roastery')" not in source
    assert "selected.brand_name" in source
    assert "|| 'Example Roastery'" in source
