"""Letter series on bag labels, without joining the shop's numeric drop sequence."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import ImageDraw
from pydantic import ValidationError

from maillard.core.config import MaillardConfig
from maillard.db.models import PresaleDrop, Roast
from maillard.db.schemas import RoastUpdate
from maillard.drop_label import (
    LABEL_ERROR,
    NUMBER_ERROR,
    SERIES_ERROR,
    format_drop_heading,
    parse_drop_label,
)
from maillard.mcp.state import ApplicationState
from maillard.sync.site import build_roast_payload
from maillard.web.label_service import LabelData, build_preview_png, build_zpl
from maillard.web.label_styles import get_theme
from maillard.web.routes import create_router


def test_parse_drop_label():
    assert parse_drop_label("RB001") == ("RB", 1)
    assert parse_drop_label(" rb012 ") == ("RB", 12)
    assert parse_drop_label("004") == (None, 4)
    assert parse_drop_label("4") == (None, 4)
    assert parse_drop_label(4) == (None, 4)
    assert parse_drop_label("") == (None, None)
    assert parse_drop_label("   ") == (None, None)
    assert parse_drop_label(None) == (None, None)
    assert parse_drop_label("ABCD001") == ("ABCD", 1)

    with pytest.raises(ValueError, match=SERIES_ERROR):
        parse_drop_label("ABCDE001")
    for garbage in ("RB-001", "1RB", "RB 001", "DROP #RB001", "R1B001", "rb_001"):
        with pytest.raises(ValueError, match=LABEL_ERROR):
            parse_drop_label(garbage)
    for bad_number in ("0", "000", "RB000", "1000000", 0):
        with pytest.raises(ValueError, match=NUMBER_ERROR):
            parse_drop_label(bad_number)


def test_heading_keeps_plain_numbers_and_prefixes_series():
    assert format_drop_heading(4, None, branded=True) == "DROP #004"
    assert format_drop_heading(4, "", branded=True) == "DROP #004"
    assert format_drop_heading(1, "RB", branded=True) == "DROP #RB001"
    assert format_drop_heading(4, "ABCDE", branded=True) == "DROP #004"
    assert format_drop_heading(None, "RB", branded=True) == "DROP #001"
    assert format_drop_heading(None, None, branded=False) == "ROAST"


def test_schema_rejects_bad_series_and_uppercases():
    assert RoastUpdate(drop_series="rb").drop_series == "RB"
    assert RoastUpdate(drop_series="").drop_series is None
    assert RoastUpdate(drop_series=None).drop_series is None
    with pytest.raises(ValidationError):
        RoastUpdate(drop_series="RB001")
    with pytest.raises(ValidationError):
        RoastUpdate(drop_series="ABCDE")


def _drawn(data: LabelData, style: str, build):
    rendered: list[str] = []
    text = ImageDraw.ImageDraw.text

    def capture(draw, xy, value, *args, **kwargs):
        rendered.append(value)
        return text(draw, xy, value, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", capture):
        output = build(data, get_theme(style))
    return rendered, output


@pytest.mark.parametrize("build", [build_preview_png, build_zpl])
def test_png_and_zpl_render_series_and_plain_numbers(build):
    plain = LabelData(
        roast_id="r", coffee_name="Example Lot", bond_number=4, roast_name="sample-name"
    )
    series = LabelData(
        roast_id="r",
        coffee_name="Example Lot",
        bond_number=1,
        drop_series="RB",
        roast_name="sample-name",
    )
    plain_text, plain_out = _drawn(plain, "drop", build)
    series_text, series_out = _drawn(series, "drop", build)
    assert "DROP #004" in plain_text
    assert "DROP #RB001" not in plain_text
    assert "DROP #RB001" in series_text
    assert "DROP #001" not in series_text
    assert plain_out != series_out
    if build is build_zpl:
        assert plain_out.startswith("^XA") and "^GFA" in plain_out
        assert series_out.startswith("^XA") and "^GFA" in series_out


def test_bond_strip_shows_series_and_leaves_plain_numbers():
    data = LabelData(roast_id="r", coffee_name="Example Lot", bond_number=7)
    plain, _ = _drawn(data, "bond", build_preview_png)
    data.drop_series = "RB"
    lettered, _ = _drawn(data, "bond", build_preview_png)
    assert "7" in plain
    assert "007" not in plain
    assert "RB007" not in plain
    assert "RB007" in lettered


async def test_series_is_withheld_from_store_sequence(mongo_db):  # noqa: ARG001
    roast = Roast(status="completed", code="K7Q2MX", bond_number=1, drop_series="RB")
    payload = build_roast_payload(roast, None, None, version="test")
    assert payload["roast"]["bond_number"] is None
    assert "drop_series" not in payload["roast"]
    assert payload["roast"]["drop"] is None
    assert roast.bond_number == 1 and roast.drop_series == "RB"

    roast.drop_series = None
    roast.bond_number = 4
    assert build_roast_payload(roast, None, None, version="test")["roast"]["bond_number"] == 4

    roast.drop_series = "RB"
    roast.bond_number = 1
    drop = SimpleNamespace(id="drop-1", drop_number=9)
    linked = build_roast_payload(roast, None, None, version="test", drop=drop)
    assert linked["roast"]["bond_number"] is None
    assert linked["roast"]["drop"] == {"maillard_id": "drop-1", "drop_number": 9}

    roast.drop_series = "ABCDE"
    roast.bond_number = 4
    assert build_roast_payload(roast, None, None, version="test")["roast"]["bond_number"] == 4


@pytest.fixture
async def api(mongo_db):  # noqa: ARG001
    app = FastAPI()
    app.state.maillard = ApplicationState(config=MaillardConfig())
    app.include_router(create_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_drop_label_persists_and_rejects_garbage(api):
    roast = Roast(status="in_progress")
    await roast.insert()
    detail = (await api.get(f"/api/roasts/{roast.id}")).json()
    assert detail["bond_number"] is None
    assert detail["drop_series"] is None

    saved = (await api.put(f"/api/roasts/{roast.id}", json={"drop_label": "rb001"})).json()
    assert saved["bond_number"] == 1
    assert saved["drop_series"] == "RB"
    assert saved["drop_id"] is None
    assert await PresaleDrop.find_all().to_list() == []

    numbered = (await api.put(f"/api/roasts/{roast.id}", json={"drop_label": "004"})).json()
    assert numbered["bond_number"] == 4
    assert numbered["drop_series"] is None

    bad = (await api.put(f"/api/roasts/{roast.id}", json={"drop_label": "RB-001"})).json()
    assert bad["status"] == "error"
    assert bad["error"] == LABEL_ERROR
    stored = await Roast.get(roast.id)
    assert stored is not None
    assert stored.bond_number == 4
    assert stored.drop_series is None

    cleared = (await api.put(f"/api/roasts/{roast.id}", json={"drop_label": ""})).json()
    assert cleared["bond_number"] is None
    assert cleared["drop_series"] is None


async def test_preview_uses_saved_series(api):
    roast = Roast(status="in_progress", bond_number=1, drop_series="RB")
    await roast.insert()
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png") as render:
        preview = (await api.post("/api/labels/preview", json={"roast_id": roast.id})).json()
    assert preview["status"] == "ok"
    data = render.call_args.args[0]
    assert data.bond_number == 1
    assert data.drop_series == "RB"
