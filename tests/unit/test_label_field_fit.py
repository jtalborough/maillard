"""Bounded label metadata and request-only text overrides."""
import io
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from maillard.db.models import Coffee, Roast
from maillard.web import label_service
from maillard.web.label_service import LabelData, _find_font, _fit_cell_text, build_preview_png
from maillard.web.label_styles import get_theme, load_themes
from tests.unit.test_labels_roast_code import label_app as _label_app

label_app = _label_app


def fit(text, width=328):
    draw = ImageDraw.Draw(Image.new("RGB", (1200, 1200)))
    font, lines = _fit_cell_text(draw, text, width, lambda size: _find_font(size, bold=True))
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        assert box[2] - box[0] <= width
        assert box[2] <= width
    return font, lines


def test_short_cell():
    font, lines = fit("HONEY")
    assert font.size == 32
    assert lines == ["HONEY"]


def test_medium_cell_shrinks():
    draw = ImageDraw.Draw(Image.new("RGB", (1200, 1200)))
    text = "MEDIUM LENGTH VALUE"
    width = draw.textbbox((0, 0), text, font=_find_font(27, bold=True))[2]
    font, lines = fit(text, width)
    assert 22 <= font.size < 32
    assert lines == [text]


def test_origin_cell():
    font, lines = fit("COLOMBIA, VEREDA SAN FRANCISCO")
    assert 1 <= len(lines) <= 2
    assert font.size >= 22
    if len(lines) == 2:
        assert font.size == 22


@pytest.mark.parametrize("text", ["LONG ORIGIN AND MANY OTHER WORDS " * 30, "W" * 200])
def test_truncated_cell(text):
    font, lines = fit(text)
    assert font.size == 22
    assert len(lines) == 2
    assert lines[-1].endswith("…")


@pytest.mark.parametrize("text", [None, "", "  "])
def test_empty_cell(text):
    assert fit(text)[1] == []


def test_drop_value_bounds_and_row_spacing(monkeypatch):
    theme = get_theme("drop", load_themes())
    data = LabelData(
        roast_id="test", origin="Colombia, Vereda San Francisco", processing="Honey",
        coffee_name="Colombia Honey Vereda San Francisco", varietal="Bourbon " * 20,
        roast_level="Medium " * 20, roast_date="October 04, 2026", weight_display="12.8 OZ (365 g)",
        qr_url="https://roastery.example.com/r/K7Q2MX", roast_code="K7Q2MX",
        short_url="roastery.example.com/r/K7Q2MX",
    )
    original_fit = label_service._fit_cell_text
    original_text = ImageDraw.ImageDraw.text
    cells = []
    boxes = []
    labels = {}
    rules = []
    original_rectangle = ImageDraw.ImageDraw.rectangle

    def capture_rectangle(draw, xy, *args, **kwargs):
        rules.append(xy[0][1])
        return original_rectangle(draw, xy, *args, **kwargs)

    def capture_fit(*args, **kwargs):
        result = original_fit(*args, **kwargs)
        cells.append(result)
        return result

    def capture_text(draw, xy, value, *args, **kwargs):
        font = kwargs.get("font")
        if value in {"ORIGIN", "PROCESS", "WEIGHT", "VARIETAL", "ROAST", "ROASTED"}:
            labels[value] = xy[1]
        if cells and font is cells[-1][0] and value in cells[-1][1]:
            boxes.append((len(cells) - 1, draw.textbbox(xy, value, font=font)))
        return original_text(draw, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", capture_rectangle)
    monkeypatch.setattr(label_service, "_fit_cell_text", capture_fit)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    png = build_preview_png(data, theme)
    image = Image.open(io.BytesIO(png))
    image.load()
    assert image.size == (int(theme.width_inches * theme.dpi), int(theme.height_inches * theme.dpi))
    assert len(cells) == 6
    assert len(boxes) == sum(len(lines) for _, lines in cells)
    right_bounds = (388, 758, image.width - 60)
    for index, box in boxes:
        assert box[2] <= right_bounds[index % 3]
        if index < 3:
            assert box[3] < labels["VARIETAL"]
        else:
            assert box[3] < rules[-1]
    assert labels["VARIETAL"] - labels["ORIGIN"] > 74


@pytest.mark.parametrize("style", ["classic", "drop"])
def test_long_values_keep_footer_in_canvas(style):
    data = LabelData(
        roast_id="test", coffee_name="Colombia Honey Vereda San Francisco",
        origin="Colombia, Vereda San Francisco " * 10, processing="Honey " * 30,
        varietal="Bourbon " * 30, roast_level="Medium " * 30,
        roast_date="October 04, 2026", weight_display="12.8 OZ (365 g) " * 30,
        qr_url="https://roastery.example.com/r/K7Q2MX", roast_code="K7Q2MX",
        short_url="roastery.example.com/r/K7Q2MX",
    )
    theme = get_theme(style, load_themes())
    original_fit = label_service._fit_cell_text
    original_text = ImageDraw.ImageDraw.text
    cells = []
    bounds = []

    def capture_fit(draw, text, width, factory, **kwargs):
        font, lines = original_fit(draw, text, width, factory, **kwargs)
        cells.append((font, lines, width))
        return font, lines

    def capture_text(draw, xy, text, *args, **kwargs):
        font = kwargs.get("font")
        if cells and font is cells[-1][0] and text in cells[-1][1]:
            _, _, width = cells[-1]
            box = draw.textbbox(xy, text, font=font)
            assert box[2] <= xy[0] + width
        if font:
            bounds.append(draw.textbbox(xy, text, font=font, anchor=kwargs.get("anchor")))
        return original_text(draw, xy, text, *args, **kwargs)

    with patch.object(label_service, "_fit_cell_text", capture_fit), patch.object(
        ImageDraw.ImageDraw, "text", capture_text,
    ), patch.object(label_service, "_draw_qr", wraps=label_service._draw_qr) as qr:
        png = build_preview_png(data, theme)
    image = Image.open(io.BytesIO(png))
    assert all(0 <= box[1] < box[3] < image.height for box in bounds)
    _, qr_y, qr_size = qr.call_args.args[3:6]
    assert qr_size > 0
    assert 0 <= qr_y < qr_y + qr_size < image.height
    assert len(cells) == (6 if style == "drop" else 4)


@pytest.mark.asyncio
async def test_request_label_overrides(label_app):
    client, state, _lp, _send = label_app
    coffee = Coffee(name="Default Coffee", origin="Default Origin")
    await coffee.insert()
    roast = Roast(code="K7Q2MX", status="completed", coffee_id=coffee.id,
                  roasted_weight_grams=1020, bags_packed=3)
    await roast.insert()
    payload = {"roast_id": roast.id, "override": True, "net_weight_grams": 365,
               "overrides": {"coffee_name": "  Edited Title  ", "origin": "  Edited Origin  "}}
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png") as render:
        assert (await client.post("/api/labels/preview", json=payload)).json()["status"] == "ok"
        assert render.call_args.args[0].coffee_name == "Edited Title"
        assert render.call_args.args[0].origin == "Edited Origin"
        state.config.zebra_transport = "cups"
        state.config.zebra_cups_name = "mock-cups"
        assert (await client.post("/api/labels/print", json=payload)).json()["status"] == "ok"
        assert render.call_args.args[0].coffee_name == "Edited Title"
        assert render.call_args.args[0].origin == "Edited Origin"
    state.config.zebra_transport = "tcp"
    with patch("maillard.web.label_service.build_zpl", return_value="zpl") as render:
        assert (await client.post("/api/labels/print", json=payload)).json()["status"] == "ok"
        assert render.call_args.args[0].coffee_name == "Edited Title"
        assert render.call_args.args[0].origin == "Edited Origin"
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png") as render:
        for overrides in [{"coffee_name": "  ", "origin": ""},
                          {"coffee_name": 123, "origin": None, "processing": "changed"},
                          [], None]:
            await client.post("/api/labels/preview", json={**payload, "overrides": overrides})
            data = render.call_args.args[0]
            assert data.coffee_name == "Default Coffee"
            assert data.origin == "Default Origin"
            assert data.processing is None
        await client.post("/api/labels/preview", json={**payload, "overrides": {
            "coffee_name": "x" * 150, "origin": "y" * 150,
        }})
        assert render.call_args.args[0].coffee_name == "x" * 120
        assert render.call_args.args[0].origin == "y" * 120
    saved = await Coffee.get(coffee.id)
    assert saved.name == "Default Coffee"
    assert saved.origin == "Default Origin"
