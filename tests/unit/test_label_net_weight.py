"""Per-bag net weight on the label, stored once per print."""

from unittest.mock import patch

import pytest
from PIL import ImageDraw

from maillard.db.models import Roast
from maillard.web.label_service import LabelData, build_preview_png, format_net_weight
from maillard.web.label_styles import get_theme, load_themes
from tests.unit.test_labels_roast_code import label_app as _label_app

label_app = _label_app


@pytest.mark.parametrize(
    ("grams", "text"),
    [
        (340, "11.9 OZ (340 g)"),
        (360, "12.6 OZ (360 g)"),
        (365, "12.8 OZ (365 g)"),
        (369, "13.0 OZ (369 g)"),
        (365.9, "12.8 OZ (365 g)"),
    ],
)
def test_format_net_weight(grams, text):
    assert format_net_weight(grams) == text


@pytest.mark.parametrize("grams", [49, 2001])
def test_format_net_weight_rejects_out_of_range(grams):
    with pytest.raises(ValueError):
        format_net_weight(grams)


def _drawn(data: LabelData) -> list[str]:
    texts: list[str] = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, *args, **kwargs):
        texts.append(str(text))
        return original(draw, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", capture):
        build_preview_png(data, get_theme("drop", load_themes()))
    return texts


def test_drop_label_has_no_fixed_weight():
    texts = _drawn(LabelData(roast_id="r", coffee_name="Sample Lot"))
    blob = " ".join(texts)
    assert "340" not in blob
    assert "12OZ" not in blob
    assert "NET WT" in texts
    assert "-" in texts


def test_drop_label_prints_entered_weight():
    texts = _drawn(
        LabelData(roast_id="r", coffee_name="Sample Lot", weight_display=format_net_weight(365))
    )
    assert "NET WT" in texts
    assert "12.8 OZ (365 G)" in texts


@pytest.mark.asyncio
async def test_print_stores_each_bag_once(label_app):
    client, _state, _lp, send = label_app
    roast = await Roast(
        code="K7Q2MX",
        status="completed",
        roasted_weight_grams=1000,
        bags_packed=2,
        site_synced_at="2026-10-01T00:00:00Z",
        site_synced_code="K7Q2MX",
        site_published=True,
        weight_display="saved text",
    ).insert()
    with patch("maillard.web.label_service.build_preview_png", return_value=b"png") as render:
        preview = await client.post("/api/labels/preview", json={"roast_id": roast.id})
        assert preview.status_code == 200
        assert render.call_args.args[0].weight_display == "-"
        weighed = await client.post(
            "/api/labels/preview",
            json={"roast_id": roast.id, "style": "bond", "net_weight_grams": 365},
        )
        assert weighed.status_code == 200
        assert render.call_args.args[0].weight_display == "NET WT 12.8 OZ (365 g)"
    assert (await Roast.get(roast.id)).bag_weights_grams == []

    with patch("maillard.web.label_service.build_zpl", return_value="^XA^XZ"):
        missing = await client.post("/api/labels/print", json={"roast_id": roast.id})
        assert missing.status_code == 422
        assert send.call_count == 0

        low = await client.post(
            "/api/labels/print", json={"roast_id": roast.id, "net_weight_grams": 49}
        )
        assert low.status_code == 422

        first = await client.post(
            "/api/labels/print", json={"roast_id": roast.id, "net_weight_grams": 365.9}
        )
        assert first.status_code == 200
        assert [int(g) for g in (await Roast.get(roast.id)).bag_weights_grams] == [365]

        second = await client.post(
            "/api/labels/print", json={"roast_id": roast.id, "net_weight_grams": 360}
        )
        assert second.status_code == 200
        assert [int(g) for g in second.json()["bag_weights_grams"]] == [365, 360]

        reprint = await client.post(
            "/api/labels/print",
            json={"roast_id": roast.id, "net_weight_grams": 360, "reprint": True},
        )
        assert reprint.status_code == 200
        assert [int(g) for g in (await Roast.get(roast.id)).bag_weights_grams] == [365, 360]

        full = await client.post(
            "/api/labels/print", json={"roast_id": roast.id, "net_weight_grams": 361}
        )
        assert full.status_code == 409
        assert full.json()["detail"] == "All bags already labeled; reprint instead"
        assert send.call_count == 3
        assert [int(g) for g in (await Roast.get(roast.id)).bag_weights_grams] == [365, 360]
