"""Tests for label style (theme) loading and rendering."""

import os
import textwrap
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

import pytest

from maillard.web.label_service import LabelData, build_preview_png, build_zpl
from maillard.web.label_styles import (
    LabelTheme,
    _load_theme_from_yaml,
    _parse_field_specs,
    get_theme,
    load_themes,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_data() -> LabelData:
    """Sample label data for rendering tests."""
    return LabelData(
        roast_id="test-abc-123",
        coffee_name="Ethiopia Yirgacheffe",
        origin="Ethiopia",
        processing="Washed",
        varietal="Heirloom",
        roast_level="Light",
        roast_date="February 28, 2026",
        drop_temp=198.5,
        total_time="10:45",
        dtr=18.5,
        batch_size_grams=200,
        qr_url="https://roastery.example.com/r/K7Q2MX",
        roast_code="K7Q2MX",
        short_url="roastery.example.com/r/K7Q2MX",
    )


@pytest.fixture
def minimal_data() -> LabelData:
    """Minimal label data (only required fields)."""
    return LabelData(roast_id="min-123", coffee_name="Test Coffee")


@pytest.fixture
def custom_theme_dir(tmp_path: Path) -> Path:
    """Create a temp directory with a custom YAML theme."""
    theme_yaml = textwrap.dedent("""\
        id: custom-red
        name: Custom Red
        description: A test custom theme
        layout: classic

        colors:
          background: "#ff0000"
          text: "#ffffff"

        text:
          brand_name: Test Brand
          brand_tagline: Test Tagline
    """)
    theme_file = tmp_path / "custom-red.yaml"
    theme_file.write_text(theme_yaml)
    return tmp_path


# ---------------------------------------------------------------------------
# LabelTheme dataclass tests
# ---------------------------------------------------------------------------

class TestLabelTheme:
    """Tests for LabelTheme dataclass."""

    def test_default_values(self) -> None:
        theme = LabelTheme(id="test", name="Test")
        assert theme.layout == "classic"
        assert theme.width_inches == 4.0
        assert theme.height_inches == 4.0
        assert theme.dpi == 300
        assert theme.heading_family == "Arial"
        assert theme.body_family == "Arial"
        assert theme.brand_name == "Example Roastery"
        assert theme.brand_tagline == "Roasted with Maillard"
        assert theme.qr_enabled is True
        assert theme.corner_marks is False
        assert theme.diagonal_clip is False

    def test_color_lookup_with_default(self) -> None:
        theme = LabelTheme(id="t", name="T", colors={"bg": "#fff"})
        assert theme.color("bg") == "#fff"
        assert theme.color("missing") == "#000000"
        assert theme.color("missing", "#aabbcc") == "#aabbcc"


# ---------------------------------------------------------------------------
# FieldSpec + parser tests
# ---------------------------------------------------------------------------

class TestFieldSpec:
    """Tests for FieldSpec and YAML parsing."""

    def test_parse_field_specs_basic(self) -> None:
        raw = [
            {"label": "Process", "source": "processing"},
            {"label": "Temp", "source": "drop_temp", "format": "{:.1f}°C"},
        ]
        specs = _parse_field_specs(raw)
        assert len(specs) == 2
        assert specs[0].label == "Process"
        assert specs[0].source == "processing"
        assert specs[0].format == "{}"
        assert specs[1].format == "{:.1f}°C"

    def test_parse_field_specs_skips_invalid(self) -> None:
        raw = [
            {"label": "Good", "source": "processing"},
            {"label": "Bad"},  # missing source
            "not a dict",
            {"source": "ok_no_label"},
        ]
        specs = _parse_field_specs(raw)
        assert len(specs) == 2  # only items with "source" key

    def test_parse_field_specs_empty(self) -> None:
        assert _parse_field_specs([]) == []


# ---------------------------------------------------------------------------
# YAML loading tests
# ---------------------------------------------------------------------------

class TestLoadThemeFromYaml:
    """Tests for loading individual YAML theme files."""

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            id: test-theme
            name: Test Theme
            description: For testing
            layout: craft

            colors:
              background: "#ffffff"
              name_block_bg: "#000000"

            canvas:
              width_inches: 3.0
              dpi: 150

            name_block:
              diagonal_clip: true
              ghost_b_enabled: true

            fonts:
              heading_family: Oswald
              body_family: Space Mono

            text:
              brand_name: My Brand

            fields:
              eyebrow: "{origin} · {varietal}"
              specs_left:
                - {label: Process, source: processing}
              strip:
                - {label: Temp, source: drop_temp, format: "{:.1f}°C"}

            corner_marks: true
            qr_enabled: false
        """)
        f = tmp_path / "test.yaml"
        f.write_text(yaml_content)

        theme = _load_theme_from_yaml(f)
        assert theme is not None
        assert theme.id == "test-theme"
        assert theme.name == "Test Theme"
        assert theme.layout == "craft"
        assert theme.color("background") == "#ffffff"
        assert theme.color("name_block_bg") == "#000000"
        assert theme.width_inches == 3.0
        assert theme.dpi == 150
        assert theme.diagonal_clip is True
        assert theme.ghost_b_enabled is True
        assert theme.heading_family == "Oswald"
        assert theme.body_family == "Space Mono"
        assert theme.brand_name == "My Brand"
        assert theme.eyebrow == "{origin} · {varietal}"
        assert len(theme.specs_left) == 1
        assert len(theme.strip) == 1
        assert theme.corner_marks is True
        assert theme.qr_enabled is False

    def test_load_yaml_missing_id(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("name: No ID\nlayout: classic\n")
        theme = _load_theme_from_yaml(f)
        assert theme is None

    def test_load_yaml_invalid_content(self, tmp_path: Path) -> None:
        f = tmp_path / "invalid.yaml"
        f.write_text("not: valid: yaml: [[[")
        theme = _load_theme_from_yaml(f)
        assert theme is None

    def test_load_yaml_minimal(self, tmp_path: Path) -> None:
        f = tmp_path / "min.yaml"
        f.write_text("id: minimal\n")
        theme = _load_theme_from_yaml(f)
        assert theme is not None
        assert theme.id == "minimal"
        assert theme.name == "minimal"  # defaults to id


# ---------------------------------------------------------------------------
# load_themes / get_theme tests
# ---------------------------------------------------------------------------

class TestLoadThemes:
    """Tests for theme loading and retrieval."""

    def test_loads_builtin_themes(self) -> None:
        themes = load_themes()
        assert "classic" in themes
        assert "craft-parchment" in themes
        assert "craft-bw" in themes
        assert "bond" in themes
        assert len(themes) >= 4

    def test_builtin_theme_properties(self) -> None:
        themes = load_themes()

        classic = themes["classic"]
        assert classic.layout == "classic"
        assert classic.name == "Classic"

        parchment = themes["craft-parchment"]
        assert parchment.layout == "craft"
        assert parchment.heading_family == "Oswald"
        assert len(parchment.strip) == 4

        bw = themes["craft-bw"]
        assert bw.layout == "craft"
        assert bw.color("name_block_bg") == "#000000"
        assert bw.corner_marks is True

    def test_custom_dir_overrides(self, custom_theme_dir: Path) -> None:
        themes = load_themes(str(custom_theme_dir))
        assert "custom-red" in themes
        assert themes["custom-red"].color("background") == "#ff0000"
        # Built-ins still present
        assert "classic" in themes

    def test_custom_dir_override_by_id(self, tmp_path: Path) -> None:
        """Custom theme with same ID as built-in overrides it."""
        override = tmp_path / "classic.yaml"
        override.write_text(textwrap.dedent("""\
            id: classic
            name: My Classic
            layout: classic
            text:
              brand_name: Override Brand
        """))
        themes = load_themes(str(tmp_path))
        assert themes["classic"].brand_name == "Override Brand"

    def test_nonexistent_custom_dir(self) -> None:
        themes = load_themes("/nonexistent/path/12345")
        # Should still load built-ins without error
        assert "classic" in themes

    def test_get_theme_found(self) -> None:
        themes = load_themes()
        theme = get_theme("craft-bw", themes)
        assert theme.id == "craft-bw"

    def test_get_theme_fallback_to_classic(self) -> None:
        themes = load_themes()
        theme = get_theme("nonexistent-style", themes)
        assert theme.id == "classic"

    def test_get_theme_empty_dict_returns_default(self) -> None:
        theme = get_theme("anything", {})
        assert theme.id == "classic"
        assert theme.layout == "classic"

    def test_get_theme_loads_from_disk_when_none(self) -> None:
        theme = get_theme("classic")
        assert theme.id == "classic"


# ---------------------------------------------------------------------------
# LabelData tests
# ---------------------------------------------------------------------------

class TestLabelData:
    """Tests for LabelData field access."""

    def test_get_field_existing(self, sample_data: LabelData) -> None:
        assert sample_data.get_field("origin") == "Ethiopia"
        assert sample_data.get_field("drop_temp") == "198.5"
        assert sample_data.get_field("batch_size_grams") == "200"

    def test_get_field_none(self, minimal_data: LabelData) -> None:
        assert minimal_data.get_field("origin") is None
        assert minimal_data.get_field("drop_temp") is None

    def test_get_field_nonexistent_attr(self, sample_data: LabelData) -> None:
        assert sample_data.get_field("fake_field") is None


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------

class TestRendering:
    """Tests for label rendering with different themes."""

    def test_classic_renders_png(self, sample_data: LabelData) -> None:
        themes = load_themes()
        theme = themes["classic"]
        png = build_preview_png(sample_data, theme=theme)
        assert isinstance(png, bytes)
        assert len(png) > 1000
        assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes

    def test_craft_parchment_renders_png(self, sample_data: LabelData) -> None:
        themes = load_themes()
        theme = themes["craft-parchment"]
        png = build_preview_png(sample_data, theme=theme)
        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_craft_bw_renders_png(self, sample_data: LabelData) -> None:
        themes = load_themes()
        theme = themes["craft-bw"]
        png = build_preview_png(sample_data, theme=theme)
        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_backward_compatible_no_theme(self, sample_data: LabelData) -> None:
        """build_preview_png() with no theme should still work."""
        png = build_preview_png(sample_data)
        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_minimal_data_renders(self, minimal_data: LabelData) -> None:
        """Labels should render even with minimal data (no optional fields)."""
        themes = load_themes()
        for style_id in ["classic", "craft-parchment", "craft-bw"]:
            png = build_preview_png(minimal_data, theme=themes[style_id])
            assert isinstance(png, bytes)
            assert len(png) > 500

    def test_no_qr_url_renders(self, sample_data: LabelData) -> None:
        """Labels without QR URL should render without error."""
        sample_data.qr_url = None
        themes = load_themes()
        for style_id in ["classic", "craft-bw"]:
            png = build_preview_png(sample_data, theme=themes[style_id])
            assert isinstance(png, bytes)

    def test_bond_renders_png(self, sample_data: LabelData) -> None:
        """Bond label renders with bond-specific fields."""
        sample_data.bond_number = 7
        sample_data.cupping_score = 88.5
        sample_data.flavor_notes = "blueberry, chocolate, citrus"
        sample_data.weight_display = "NET WT 12.8 OZ (365 g)"
        sample_data.qr_url = "https://roastery.example.com/bond/007"
        themes = load_themes()
        theme = themes["bond"]
        png = build_preview_png(sample_data, theme=theme)
        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_bond_renders_minimal_data(self, minimal_data: LabelData) -> None:
        """Bond label renders even without bond-specific fields."""
        themes = load_themes()
        theme = themes["bond"]
        png = build_preview_png(minimal_data, theme=theme)
        assert isinstance(png, bytes)
        assert len(png) > 500

    def test_different_styles_produce_different_images(self, sample_data: LabelData) -> None:
        """Each style should produce visually distinct output."""
        themes = load_themes()
        classic_png = build_preview_png(sample_data, theme=themes["classic"])
        craft_png = build_preview_png(sample_data, theme=themes["craft-bw"])
        assert classic_png != craft_png

    def test_render_image_dimensions(self, sample_data: LabelData) -> None:
        """Rendered image should match theme canvas dimensions."""
        import io

        from PIL import Image

        themes = load_themes()
        theme = themes["classic"]
        png = build_preview_png(sample_data, theme=theme)

        img = Image.open(io.BytesIO(png))
        expected_w = int(theme.width_inches * theme.dpi)
        expected_h = int(theme.height_inches * theme.dpi)
        assert img.size == (expected_w, expected_h)


# ---------------------------------------------------------------------------
# ZPL conversion tests
# ---------------------------------------------------------------------------

class TestZPL:
    """Tests for ZPL graphic conversion."""

    def test_build_zpl_with_theme(self, sample_data: LabelData) -> None:
        themes = load_themes()
        theme = themes["craft-bw"]
        zpl = build_zpl(sample_data, theme=theme)
        assert isinstance(zpl, str)
        assert zpl.startswith("^XA")
        assert "^GFA" in zpl
        assert zpl.strip().endswith("^XZ")

    def test_build_zpl_backward_compatible(self, sample_data: LabelData) -> None:
        """build_zpl() with no theme should still work."""
        zpl = build_zpl(sample_data)
        assert "^XA" in zpl
        assert "^GFA" in zpl


# ---------------------------------------------------------------------------
# Config integration tests
# ---------------------------------------------------------------------------

class TestConfigIntegration:
    """Tests for label config fields in MaillardConfig."""

    def test_config_default_style(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("MAILLARD_")}
        with patch.dict(os.environ, clean_env, clear=True):
            from maillard.core.config import MaillardConfig
            config = MaillardConfig(_env_file=None)
            assert config.label_default_style == "drop"
            assert config.label_styles_dir == ""

    def test_config_env_override(self) -> None:
        with patch.dict(os.environ, {
            "MAILLARD_LABEL_DEFAULT_STYLE": "craft-bw",
            "MAILLARD_LABEL_STYLES_DIR": "/custom/themes",
        }):
            from maillard.core.config import MaillardConfig
            config = MaillardConfig()
            assert config.label_default_style == "craft-bw"
            assert config.label_styles_dir == "/custom/themes"


def test_all_style_dimensions(sample_data):
    import io

    from PIL import Image

    for theme in load_themes().values():
        sample_data.roast_code = "K7Q2MX"
        sample_data.short_url = "roastery.example.com/r/K7Q2MX"
        png = build_preview_png(sample_data, theme)
        assert Image.open(io.BytesIO(png)).size == (1200, 1200)


def test_label_data_and_themes_have_no_curve_fields():
    assert {"plan_curve", "actual_curve"}.isdisjoint(field.name for field in fields(LabelData))
    assert "mini_curve_enabled" not in {field.name for field in fields(LabelTheme)}
    for theme in load_themes().values():
        assert not hasattr(theme, "mini_curve_enabled")


@pytest.mark.parametrize("style", ["classic", "craft-bw", "drop"])
def test_qr_fallback_and_no_score(sample_data, style):
    from PIL import ImageDraw

    text = ImageDraw.ImageDraw.text
    rendered = []

    def capture(draw, xy, value, *args, **kwargs):
        rendered.append(value)
        return text(draw, xy, value, *args, **kwargs)

    sample_data.cupping_score = 87.5
    with patch.object(ImageDraw.ImageDraw, "text", capture):
        build_preview_png(sample_data, get_theme(style))
    assert "K 7 Q 2 M X" in rendered
    assert sample_data.short_url in rendered
    assert not any("87.5" in value or "SCORE" in value for value in rendered)


@pytest.mark.parametrize("coffee_name", ["Ethiopia", "Ethiopia Yirgacheffe"])
def test_drop_footer_bounds(sample_data, coffee_name):
    from PIL import ImageDraw

    from maillard.web import label_service

    sample_data.coffee_name = coffee_name
    sample_data.bond_number = 12
    text = ImageDraw.ImageDraw.text
    bounds = {}

    def capture(draw, xy, value, *args, **kwargs):
        bounds[value] = draw.textbbox(xy, value, font=kwargs["font"], anchor=kwargs.get("anchor"))
        return text(draw, xy, value, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", capture), patch.object(
        label_service, "_draw_qr", wraps=label_service._draw_qr,
    ) as qr:
        build_preview_png(sample_data, get_theme("drop"))

    qr_x, qr_y, qr_size = qr.call_args.args[3:6]
    code_box = bounds["K 7 Q 2 M X"]
    url_box = bounds[sample_data.short_url]
    brand_box = bounds["EXAMPLE ROASTERY"]
    assert qr_size == 240
    assert 60 <= qr_x < qr_x + qr_size <= 1140
    assert qr_y + qr_size < code_box[1]
    assert code_box[3] < url_box[1]
    assert url_box[3] < brand_box[1]
    for box in [code_box, url_box, brand_box]:
        assert 60 <= box[0] < box[2] <= 1140
        assert 0 <= box[1] < box[3] < 1154


def test_brand_name_override_applies_to_all_themes():
    themes = load_themes(brand_name="Test Roasters")
    assert themes
    assert {theme.brand_name for theme in themes.values()} == {"Test Roasters"}


def test_brand_name_defaults_to_yaml_value():
    assert load_themes()["classic"].brand_name == "Example Roastery"
