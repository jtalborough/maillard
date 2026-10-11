"""Label style (theme) loading from YAML files.

Each YAML theme describes colors, fonts, text, and field layout for a label.
Multiple themes can share the same Python layout renderer (e.g. "craft").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_logger = logging.getLogger(__name__)

# Built-in styles ship alongside this module
_BUILTIN_DIR = Path(__file__).parent / "label-styles"


@dataclass
class FieldSpec:
    """A single field in the label (spec row or strip column)."""

    label: str
    source: str
    format: str = "{}"


@dataclass
class LabelTheme:
    """Complete label theme loaded from YAML."""

    id: str
    name: str
    description: str = ""
    layout: str = "classic"

    # Colors (all CSS-style hex strings)
    colors: dict[str, str] = field(default_factory=dict)

    # Canvas
    width_inches: float = 4.0
    height_inches: float = 4.0
    dpi: int = 300
    border_width: int = 2

    # Name block options
    diagonal_clip: bool = False
    clip_breakpoint: float = 0.38
    ghost_b_enabled: bool = False

    # Font families (display names, resolved to paths at render time)
    heading_family: str = "Arial"
    body_family: str = "Arial"

    # Brand text
    brand_name: str = "Example Roastery"
    brand_tagline: str = "Roasted with Maillard"
    qr_hint: str = "Scan to Cup"

    # Field definitions
    eyebrow: str = ""
    specs_left: list[FieldSpec] = field(default_factory=list)
    specs_right: list[FieldSpec] = field(default_factory=list)
    strip: list[FieldSpec] = field(default_factory=list)

    # Brand variant ("primary" or "hrs") — controls logo selection in drop layout
    brand_variant: str = "primary"
    logo_path: str = ""  # optional override; empty = use brand_variant default

    # Toggles
    corner_marks: bool = False
    qr_enabled: bool = True
    logo_enabled: bool = True

    def color(self, key: str, default: str = "#000000") -> str:
        """Get a color by key with fallback."""
        return self.colors.get(key, default)


def _parse_field_specs(raw: list[dict]) -> list[FieldSpec]:
    """Parse a list of field spec dicts into FieldSpec objects."""
    result: list[FieldSpec] = []
    for item in raw:
        if isinstance(item, dict) and "source" in item:
            result.append(
                FieldSpec(
                    label=item.get("label", ""),
                    source=item["source"],
                    format=item.get("format", "{}"),
                )
            )
    return result


def _load_theme_from_yaml(path: Path) -> LabelTheme | None:
    """Load a single theme from a YAML file."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "id" not in data:
            _logger.warning("Skipping invalid theme file: %s (missing 'id')", path)
            return None

        colors = data.get("colors", {})
        canvas = data.get("canvas", {})
        name_block = data.get("name_block", {})
        fonts = data.get("fonts", {})
        text = data.get("text", {})
        fields = data.get("fields", {})

        return LabelTheme(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            layout=data.get("layout", "classic"),
            colors=colors if isinstance(colors, dict) else {},
            width_inches=canvas.get("width_inches", 4.0),
            height_inches=canvas.get("height_inches", 4.0),
            dpi=canvas.get("dpi", 300),
            border_width=canvas.get("border_width", 2),
            diagonal_clip=name_block.get("diagonal_clip", False),
            clip_breakpoint=name_block.get("clip_breakpoint", 0.38),
            ghost_b_enabled=name_block.get("ghost_b_enabled", False),
            heading_family=fonts.get("heading_family", "Arial"),
            body_family=fonts.get("body_family", "Arial"),
            brand_name=text.get("brand_name", "Example Roastery"),
            brand_tagline=text.get("brand_tagline", "Roasted with Maillard"),
            qr_hint=text.get("qr_hint", "Scan to Cup"),
            eyebrow=fields.get("eyebrow", ""),
            specs_left=_parse_field_specs(fields.get("specs_left", [])),
            specs_right=_parse_field_specs(fields.get("specs_right", [])),
            strip=_parse_field_specs(fields.get("strip", [])),
            brand_variant=data.get("brand_variant", "primary"),
            logo_path=data.get("logo_path", ""),
            corner_marks=data.get("corner_marks", False),
            qr_enabled=data.get("qr_enabled", True),
            logo_enabled=data.get("logo_enabled", True),
        )
    except Exception:
        _logger.exception("Failed to load theme from %s", path)
        return None


def load_themes(custom_dir: str = "") -> dict[str, LabelTheme]:
    """Load all themes: built-in first, then custom overrides.

    Args:
        custom_dir: Optional path to directory with additional YAML themes.
                    Themes with the same ID override built-in themes.

    Returns:
        Dict of themes keyed by style ID.
    """
    themes: dict[str, LabelTheme] = {}

    # 1. Built-in themes
    if _BUILTIN_DIR.is_dir():
        for path in sorted(_BUILTIN_DIR.glob("*.yaml")):
            theme = _load_theme_from_yaml(path)
            if theme:
                themes[theme.id] = theme

    # 2. Custom themes (override by ID)
    if custom_dir:
        custom_path = Path(custom_dir)
        if custom_path.is_dir():
            for path in sorted(custom_path.glob("*.yaml")):
                theme = _load_theme_from_yaml(path)
                if theme:
                    themes[theme.id] = theme
        else:
            _logger.warning("Custom label styles directory not found: %s", custom_dir)

    return themes


def get_theme(style_id: str, themes: dict[str, LabelTheme] | None = None) -> LabelTheme:
    """Get a specific theme by ID with fallback to 'classic'.

    Args:
        style_id: The theme ID to look up.
        themes: Pre-loaded themes dict. If None, loads from disk.

    Returns:
        The requested theme, or 'classic' if not found, or a minimal default.
    """
    if themes is None:
        themes = load_themes()

    if style_id in themes:
        return themes[style_id]

    if "classic" in themes:
        _logger.warning("Theme '%s' not found, falling back to 'classic'", style_id)
        return themes["classic"]

    _logger.warning("No themes found, using minimal default")
    return LabelTheme(id="classic", name="Classic", layout="classic")
