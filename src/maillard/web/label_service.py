"""Label generation and printing for roasted coffee bags.

Renders labels as images (Pillow) with selectable styles/themes.
Two layout renderers:
  - classic: Centered name, two-column details, circled-B logo
  - craft:   Diagonal name block, spec columns, data strip, footer lockup

Two print paths (CUPS preferred, TCP/ZPL fallback):
  - CUPS: Sends PNG via `lp` command — the Zebra CUPS driver handles
    conversion with optimized dithering (produces best quality)
  - TCP/ZPL: Converts PNG to ^GFA bitmap and sends raw ZPL over TCP
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

_logger = logging.getLogger(__name__)

TCP_TIMEOUT = 5  # seconds

# Bundled fonts directory (shipped with the package)
_FONTS_DIR = Path(__file__).parent / "fonts"


@dataclass
class LabelData:
    """Data needed to render a coffee label."""

    roast_id: str
    coffee_name: str
    origin: str | None = None
    processing: str | None = None
    varietal: str | None = None
    altitude: int | None = None
    roast_level: str | None = None
    roast_date: str | None = None
    drop_temp: float | None = None
    total_time: str | None = None
    dtr: float | None = None
    batch_size_grams: int | None = None
    roast_code: str = ""
    short_url: str = ""
    qr_url: str | None = None
    bond_number: int | None = None
    cupping_score: float | None = None
    flavor_notes: str | None = None
    weight_display: str | None = None
    roast_name: str | None = None

    @property
    def drop_number(self) -> int | None:
        """Alias for bond_number — drop is the new terminology."""
        return self.bond_number

    @drop_number.setter
    def drop_number(self, value: int | None) -> None:
        self.bond_number = value

    def get_field(self, source: str) -> str | None:
        """Get a field value by name, returning None if missing."""
        if source == "drop_number":
            source = "bond_number"
        val = getattr(self, source, None)
        if val is None:
            return None
        return str(val)


def _fit_cell_text(draw, text, max_width, font_factory, max_size=32, min_size=22,
                   max_lines=2):
    """Shrink, wrap, and truncate a value within its cell's ink width."""
    text = " ".join((text or "").split())

    def fits(value, font):
        left, _, right, _ = draw.textbbox((0, 0), value, font=font)
        # Also reserve positive left bearings for the default drawing anchor.
        return max(right, right - left) <= max_width

    for size in range(max_size, min_size - 1, -1):
        font = font_factory(size)
        if fits(text, font):
            return font, [text] if text else []

    lines = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if fits(candidate, font):
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        for char in word:
            if not fits(current + char, font):
                if current:
                    lines.append(current)
                    current = ""
                if not fits(char, font):
                    continue
            current += char
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        while last and not fits(last + "…", font):
            last = last[:-1].rstrip()
        lines[-1] = last + "…" if fits(last + "…", font) else ""
    return font, lines


def _draw_qr_text(draw, data, cx, y, width, color="black"):
    """Fit the human-readable QR fallback into its reserved column."""
    for text, size, bold in [(" ".join(data.roast_code), 26, True), (data.short_url, 18, False)]:
        font = _find_font(size, bold=bold, family="Courier New" if bold else "Arial")
        while draw.textbbox((0, 0), text, font=font)[2] > width and size > 10:
            size -= 1
            font = _find_font(size, bold=bold, family="Courier New" if bold else "Arial")
        draw.text((cx, y), text, font=font, fill=color, anchor="mt")
        y += size + 8


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------


def _find_font(size: int, bold: bool = False, family: str = "Arial"):
    """Find a font by family name, checking bundled fonts first.

    Fallback chain: bundled fonts → system fonts → Pillow default.
    """
    from PIL import ImageFont

    # Bundled font lookup
    bundled = _get_bundled_font(family, bold)
    if bundled:
        try:
            font = ImageFont.truetype(str(bundled), size)
            # For variable fonts (Oswald), set weight
            if bold and "Variable" in bundled.name:
                with contextlib.suppress(Exception):
                    font.set_variation_by_name("Bold")
            return font
        except Exception:
            pass

    # System font lookup
    system_paths = _get_system_font_paths(family, bold)
    for p in system_paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue

    return ImageFont.load_default(size=size)


def _get_bundled_font(family: str, bold: bool) -> Path | None:
    """Map a font family name to a bundled TTF file."""
    family_lower = family.lower()

    if family_lower == "oswald":
        p = _FONTS_DIR / "Oswald-Variable.ttf"
        return p if p.exists() else None

    if family_lower in ("space mono", "spacemono"):
        name = "SpaceMono-Bold.ttf" if bold else "SpaceMono-Regular.ttf"
        p = _FONTS_DIR / name
        return p if p.exists() else None

    return None


def _get_system_font_paths(family: str, bold: bool) -> list[str]:
    """Get system font paths for common families."""
    family_lower = family.lower()

    if family_lower == "impact":
        return [
            "/System/Library/Fonts/Supplemental/Impact.ttf",
            "/Library/Fonts/Impact.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
        ]

    if family_lower in ("arial black",):
        return [
            "/System/Library/Fonts/Supplemental/Arial Black.ttf",
            "/Library/Fonts/Arial Black.ttf",
        ]

    if bold:
        if family_lower == "arial":
            return [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/Library/Fonts/Arial Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
        # Generic bold fallback
        return [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]

    if family_lower == "arial":
        return [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

    # Generic regular fallback
    return [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]


def _text_size(draw, text: str, font) -> tuple[int, int]:
    """Get (width, height) of rendered text."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _fit_text_lines(draw, text: str, font, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        tw, _ = _text_size(draw, test, font)
        if tw > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def _auto_fit_name(draw, name: str, max_width: int, max_height: int,
                   family: str = "Arial", line_gap: int = 10):
    """Find the largest bold font size that fits the name in the given area."""
    for size in range(130, 18, -2):
        font = _find_font(size, bold=True, family=family)
        lines = _fit_text_lines(draw, name, font, max_width)
        total_h = sum(_text_size(draw, line, font)[1] + line_gap for line in lines)
        if total_h <= max_height and len(lines) <= 3:
            return font, lines
    font = _find_font(20, bold=True, family=family)
    return font, [name]


# ---------------------------------------------------------------------------
# Drawing helpers (shared across renderers)
# ---------------------------------------------------------------------------


def _draw_logo_b(draw, cx: int, cy: int, radius: int,
                 border_color: str = "black", fill_color: str = "black") -> None:
    """Draw a transit-style circled B — thick circle, bold B centered."""
    stroke = max(4, radius // 8)
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        outline=border_color,
        width=stroke,
    )
    font_size = int(radius * 1.3)
    font = _find_font(font_size, bold=True, family="Arial Black")
    bbox = draw.textbbox((0, 0), "B", font=font)
    gw = bbox[2] - bbox[0]
    gh = bbox[3] - bbox[1]
    ox = bbox[0]
    oy = bbox[1]
    bx = cx - ox - gw // 2
    by = cy - oy - gh // 2
    draw.text((bx, by), "B", fill=fill_color, font=font)


def _draw_qr(img, draw, qr_url: str, x: int, y: int, size: int,
             hint_text: str = "Scan to cup", hint_color: str = "#666666",
             fg: str = "black", bg: str = "white") -> None:
    """Draw a QR code and hint text at the given position."""
    import qrcode
    from PIL import Image

    qr = qrcode.QRCode(version=None, box_size=6, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=fg, back_color=bg).convert("RGB")
    qr_img = qr_img.resize((size, size), Image.NEAREST)
    img.paste(qr_img, (x, y))

    if hint_text:
        hint_font = _find_font(16)
        htw, _ = _text_size(draw, hint_text, hint_font)
        draw.text((x + (size - htw) // 2, y + size + 2),
                  hint_text, fill=hint_color, font=hint_font)


# ---------------------------------------------------------------------------
# Classic layout renderer
# ---------------------------------------------------------------------------


def _render_classic(data: LabelData, theme) -> bytes:
    """Render the classic centered layout (original Example Roastery design)."""
    from PIL import Image, ImageDraw

    dpi = theme.dpi
    w_px = int(theme.width_inches * dpi)
    h_px = int(theme.height_inches * dpi)
    pad = int(0.12 * dpi)

    bg = theme.color("background", "#ffffff")
    text_color = theme.color("text", "#000000")
    sep_color = theme.color("separator", "#000000")

    img = Image.new("RGB", (w_px, h_px), bg)
    draw = ImageDraw.Draw(img)
    usable_w = w_px - 2 * pad
    col_mid = w_px // 2
    y = pad

    heading_family = theme.heading_family

    def _sep(yy: int, style: str = "thin") -> None:
        width = 2 if style == "thin" else theme.border_width
        draw.line([(pad, yy), (w_px - pad, yy)], fill=sep_color, width=width)

    # === TOP BORDER ===
    _sep(y, "thick")

    # === COFFEE NAME (big, bold, centered, auto-fitted) ===
    name_area_h = int(h_px * 0.24)
    name_font, name_lines = _auto_fit_name(
        draw, data.coffee_name.upper(), usable_w, name_area_h,
        family=heading_family,
    )

    line_metrics = [_text_size(draw, line, name_font) for line in name_lines]
    text_block_h = sum(th for _, th in line_metrics) + (len(line_metrics) - 1) * 10
    name_zone = text_block_h + 90
    margin = (name_zone - text_block_h) // 2

    y += margin
    for i, line in enumerate(name_lines):
        tw, th = line_metrics[i]
        x = (w_px - tw) // 2
        draw.text((x, y), line, fill=text_color, font=name_font)
        y += th + (10 if i < len(name_lines) - 1 else 0)
    y += margin

    # Roast name below coffee name (lighter weight)
    if data.roast_name:
        rn_font = _find_font(34, family=theme.body_family)
        rn_tw, rn_th = _text_size(draw, data.roast_name, rn_font)
        draw.text(((w_px - rn_tw) // 2, y), data.roast_name,
                  fill=text_color, font=rn_font)
        y += rn_th + 8

    _sep(y)
    y += 14

    # === DETAIL ROWS (two-column layout) ===
    detail_bold = _find_font(32, bold=True, family=theme.body_family)
    row_h = 38

    details_left: list[tuple[str, str]] = []
    details_right: list[tuple[str, str]] = []

    if data.origin:
        details_left.append(("Origin", data.origin))
    if data.processing:
        if data.origin:
            details_right.append(("Process", data.processing))
        else:
            details_left.append(("Process", data.processing))
    if data.varietal:
        details_left.append(("Varietal", data.varietal))
    if data.roast_level:
        if data.varietal:
            details_right.append(("Roast", data.roast_level))
        else:
            details_left.append(("Roast", data.roast_level))

    if not details_right and len(details_left) > 1:
        half = len(details_left) // 2
        details_right = details_left[half:]
        details_left = details_left[:half]

    for i in range(max(len(details_left), len(details_right))):
        value_height = row_h
        for cells, x, cell_right in [
            (details_left, pad, col_mid - 10),
            (details_right, col_mid + 10, w_px - pad),
        ]:
            if i >= len(cells):
                continue
            label, value = cells[i]
            draw.text((x, y), f"{label}: ", fill=text_color, font=detail_bold)
            lw, _ = _text_size(draw, f"{label}: ", detail_bold)
            font, lines = _fit_cell_text(
                draw, value, cell_right - x - lw,
                lambda size: _find_font(size, family=theme.body_family),
            )
            for j, line in enumerate(lines):
                draw.text((x + lw, y + j * row_h), line, fill=text_color, font=font)
            value_height = max(value_height, len(lines) * row_h)
        y += value_height

    y += 6
    _sep(y)
    y += 14

    # === ROAST DATA SECTION (two-column) ===
    rd_font = _find_font(30, family=theme.body_family)
    rd_bold = _find_font(30, bold=True, family=theme.body_family)
    rd_row_h = 36

    rd_left: list[tuple[str, str]] = []
    rd_right: list[tuple[str, str]] = []

    if data.roast_date:
        rd_left.append(("Roasted", data.roast_date))
    if data.drop_temp is not None:
        rd_right.append(("Drop temp", f"{data.drop_temp:.1f}\u00b0C"))
    if data.total_time:
        rd_left.append(("Total time", data.total_time))
    if data.dtr is not None:
        rd_right.append(("DTR", f"{data.dtr:.1f}%"))
    if data.batch_size_grams is not None:
        rd_left.append(("Batch", f"{data.batch_size_grams}g"))

    for i in range(max(len(rd_left), len(rd_right))):
        if i < len(rd_left):
            label, value = rd_left[i]
            draw.text((pad, y), f"{label}: ", fill=text_color, font=rd_bold)
            lw, _ = _text_size(draw, f"{label}: ", rd_bold)
            draw.text((pad + lw, y), value, fill=text_color, font=rd_font)
        if i < len(rd_right):
            label, value = rd_right[i]
            draw.text((col_mid + 10, y), f"{label}: ", fill=text_color, font=rd_bold)
            lw, _ = _text_size(draw, f"{label}: ", rd_bold)
            draw.text((col_mid + 10 + lw, y), value, fill=text_color, font=rd_font)
        y += rd_row_h

    # === BOTTOM BORDER ===
    y += 10
    _sep(y, "thick")
    data_bottom_y = y

    # === FOOTER ZONE (between bottom border and label edge) ===
    footer_zone_top = data_bottom_y + 10
    footer_zone_h = h_px - pad - footer_zone_top
    footer_mid_y = footer_zone_top + footer_zone_h // 2
    footer_text_color = theme.color("footer_text", text_color)
    logo_border = theme.color("logo_border", text_color)
    logo_b = theme.color("logo_b", text_color)
    qr_hint_color = theme.color("qr_hint", "#000000")

    # --- Big centered Ⓑ logo ---
    # If QR is present, shift the logo left to make room
    logo_cx = int(w_px * 0.38) if data.qr_url and theme.qr_enabled else w_px // 2
    logo_radius = int(footer_zone_h * 0.38)
    logo_radius = max(20, min(logo_radius, int(usable_w * 0.22)))
    if logo_radius > 20:
        _draw_logo_b(draw, logo_cx, footer_mid_y, logo_radius,
                     border_color=logo_border, fill_color=logo_b)

    # --- Bottom text: brand left, tagline + QR right ---
    bottom_y = h_px - pad - 8
    footer_font = _find_font(28, bold=True, family=theme.body_family)
    tagline_font = _find_font(26, family=theme.body_family)
    _, fh = _text_size(draw, theme.brand_name, footer_font)
    draw.text((pad, bottom_y - fh), theme.brand_name,
              fill=footer_text_color, font=footer_font)

    # --- QR code + tagline group (bottom right corner, anchored to bottom) ---
    if data.qr_url and theme.qr_enabled:
        hint_font = _find_font(22, family=theme.body_family)
        hint_tw, hint_th = _text_size(draw, theme.qr_hint, hint_font)
        tw_tag, th_tag = _text_size(draw, theme.brand_tagline, tagline_font)

        qr_size = min(int(footer_zone_h * 0.48), 160)
        # Group: QR + "Scan to Cup" + "Roasted with Maillard" stacked
        group_w = max(qr_size, hint_tw, tw_tag)
        gap = 4
        group_h = qr_size + gap + hint_th + gap + th_tag
        # Anchor to bottom-right corner (aligned with "Example Roastery" baseline)
        group_x = w_px - pad - group_w
        group_y = bottom_y - group_h

        qr_x = group_x + (group_w - qr_size) // 2
        _draw_qr(img, draw, data.qr_url, qr_x, group_y, qr_size,
                 hint_text="", hint_color=qr_hint_color, bg=bg)
        _draw_qr_text(draw, data, group_x + group_w // 2, group_y + qr_size + gap,
                      group_w, qr_hint_color)
    else:
        tw_tag, th_tag = _text_size(draw, theme.brand_tagline, tagline_font)
        draw.text((w_px - pad - tw_tag, bottom_y - th_tag),
                  theme.brand_tagline,
                  fill=footer_text_color, font=tagline_font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(dpi, dpi))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Craft layout renderer (diagonal name block + specs + data strip)
# ---------------------------------------------------------------------------


def _resolve_field(data: LabelData, field_spec, default: str = "") -> str:
    """Resolve a FieldSpec against LabelData, applying format."""
    raw = data.get_field(field_spec.source)
    if raw is None:
        return default
    try:
        # Try numeric formatting if the format has :.Xf etc
        if ":" in field_spec.format:
            return field_spec.format.format(float(raw))
        return field_spec.format.format(raw)
    except (ValueError, KeyError):
        return raw


def _render_craft(data: LabelData, theme) -> bytes:
    """Render the craft layout matching HTML v2/v8 reference designs.

    Vertical space is allocated proportionally matching the HTML reference
    (name 50%, body 25%, strip 10%, footer 15%).  clip_breakpoint controls
    only the *horizontal* diagonal break position (38% in the HTML).
    """
    from PIL import Image, ImageDraw

    dpi = theme.dpi
    w_px = int(theme.width_inches * dpi)
    h_px = int(theme.height_inches * dpi)
    bg = theme.color("background", "#ffffff")

    img = Image.new("RGB", (w_px, h_px), bg)
    draw = ImageDraw.Draw(img)

    heading_family = theme.heading_family
    body_family = theme.body_family

    # Horizontal text padding (~5.4% of width, matching HTML's 28px / 520px)
    text_pad = int(0.054 * w_px)

    # ── VERTICAL SPACE ALLOCATION (proportional to HTML reference) ─────
    # HTML proportions: name 49.9%, body 25.3%, strip 10.2%, footer 14.6%
    name_block_h = int(h_px * 0.46)
    footer_h = int(h_px * 0.15)
    strip_h = int(h_px * 0.11)
    body_h = h_px - name_block_h - strip_h - footer_h  # remainder

    # ── NAME BLOCK (full-width, diagonal bottom edge) ──────────────────
    name_block_bg = theme.color("name_block_bg", "#000000")
    name_text_color = theme.color("name_text", "#ffffff")
    eyebrow_color = theme.color("eyebrow_text", "#666666")

    if theme.diagonal_clip:
        # clip_breakpoint is the HORIZONTAL position (38% in HTML)
        diag_depth = int(w_px * 0.12)
        bp_x = int(w_px * theme.clip_breakpoint)  # horizontal only
        name_poly = [
            (0, 0),
            (w_px, 0),
            (w_px, name_block_h - diag_depth),  # right edge is HIGHER
            (bp_x, name_block_h),                # breakpoint at full depth
            (0, name_block_h),                   # left edge at full depth
        ]
    else:
        diag_depth = 0
        name_poly = [(0, 0), (w_px, 0), (w_px, name_block_h), (0, name_block_h)]

    # Render name block on a separate layer so the ghost B gets clipped
    name_layer = Image.new("RGB", (w_px, h_px), bg)
    name_draw = ImageDraw.Draw(name_layer)
    name_draw.polygon(name_poly, fill=name_block_bg)

    # Ghost "B" watermark (clipped to polygon via mask)
    if theme.ghost_b_enabled:
        ghost_size = int(w_px * 0.44)
        ghost_font = _find_font(ghost_size, bold=True, family=heading_family)
        ghost_color = theme.color("ghost_b", "rgba(255,255,255,0.10)")
        ghost_fill = _rgba_to_rgb(ghost_color, name_block_bg)
        gw, _ = _text_size(name_draw, "B", ghost_font)
        gx = w_px - gw + int(w_px * 0.09)
        gy = int(-0.01 * h_px)
        name_draw.text((gx, gy), "B", fill=ghost_fill, font=ghost_font)

    # Eyebrow text
    if theme.eyebrow:
        parts = {"origin": data.origin or "", "varietal": data.varietal or "",
                 "processing": data.processing or ""}
        eyebrow_str = theme.eyebrow.format(**parts).strip(" ·")
        if eyebrow_str:
            eye_font = _find_font(22, family=body_family)
            name_draw.text(
                (text_pad, int(0.04 * name_block_h)),
                eyebrow_str.upper(), fill=eyebrow_color, font=eye_font,
            )

    # Big bold coffee name — positioned after eyebrow, above diagonal zone
    name_top = int(0.14 * name_block_h)
    name_max_w = int(w_px * 0.60)
    name_max_h = int(name_block_h * 0.52)
    name_font, name_lines = _auto_fit_name(
        name_draw, data.coffee_name.upper(), name_max_w, name_max_h,
        family=heading_family, line_gap=0,
    )

    line_metrics = [_text_size(name_draw, ln, name_font) for ln in name_lines]
    name_y = name_top
    for i, (_, th) in enumerate(line_metrics):
        name_draw.text((text_pad, name_y), name_lines[i],
                       fill=name_text_color, font=name_font)
        name_y += th

    # Roast name below coffee name in name block
    if data.roast_name:
        rn_craft_font = _find_font(30, bold=True, family=body_family)
        name_draw.text((text_pad, name_y + 6), data.roast_name,
                       fill=eyebrow_color, font=rn_craft_font)

    # QR code in name block (top right, white on black, with white border box)
    if data.qr_url and theme.qr_enabled:
        qr_size_name = min(int(name_block_h * 0.50), int(0.80 * dpi))
        qr_margin = int(text_pad * 0.8)
        qr_x_name = w_px - qr_margin - qr_size_name
        qr_y_name = int(qr_margin * 1.8)  # nudge down
        # White border box around QR
        qr_pad = 8
        name_draw.rectangle(
            [qr_x_name - qr_pad, qr_y_name - qr_pad,
             qr_x_name + qr_size_name + qr_pad, qr_y_name + qr_size_name + qr_pad],
            fill="#ffffff", outline="#ffffff", width=2)
        # QR code (black on white inside the box)
        _draw_qr(name_layer, name_draw, data.qr_url,
                 qr_x_name, qr_y_name, qr_size_name,
                 hint_text="",
                 hint_color="#ffffff",
                 fg="black", bg="#ffffff")
        _draw_qr_text(name_draw, data, qr_x_name + qr_size_name // 2,
                      qr_y_name + qr_size_name + qr_pad + 4, qr_size_name + 30, "white")

    # Composite name layer using polygon mask
    mask = Image.new("L", (w_px, h_px), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.polygon(name_poly, fill=255)
    img.paste(name_layer, mask=mask)

    # ── SPECS SECTION (two columns + center divider) ───────────────────
    # Body starts after name block with top padding (HTML: 30px/561px ≈ 5.3%)
    body_top = name_block_h + int(h_px * 0.04)

    spec_label_color = theme.color("spec_label", "#888888")
    spec_value_color = theme.color("spec_value", "#000000")
    spec_divider_color = theme.color("spec_divider", "#000000")

    spec_label_font = _find_font(34, bold=True, family=body_family)
    spec_value_font = _find_font(48, bold=True, family=heading_family)

    sample_lh = _text_size(draw, "X", spec_label_font)[1]
    sample_vh = _text_size(draw, "X", spec_value_font)[1]
    label_to_value = 4

    # Calculate row gap to fill available body space evenly
    specs_left = [fs for fs in theme.specs_left if fs.source != "cupping_score"]
    specs_right = [fs for fs in theme.specs_right if fs.source != "cupping_score"]
    strip = [fs for fs in theme.strip if fs.source != "cupping_score"]
    max_rows = max(len(specs_left), len(specs_right))
    strip_top = name_block_h + body_h  # where the strip starts
    body_avail = strip_top - body_top
    row_content_h = sample_lh + label_to_value + sample_vh
    row_gap = max(8, (body_avail - max_rows * row_content_h) // max_rows) if max_rows > 0 else 0

    divider_x = w_px // 2
    col_left_x = text_pad
    col_right_x = divider_x + int(0.055 * w_px)

    y = body_top
    spec_top_y = y
    for i in range(max_rows):
        if i < len(specs_left):
            fs = specs_left[i]
            val = _resolve_field(data, fs, "\u2014")
            draw.text((col_left_x, y), fs.label.upper(),
                      fill=spec_label_color, font=spec_label_font)
            draw.text((col_left_x, y + sample_lh + label_to_value), val.upper(),
                      fill=spec_value_color, font=spec_value_font)

        if i < len(specs_right):
            fs = specs_right[i]
            val = _resolve_field(data, fs, "\u2014")
            draw.text((col_right_x, y), fs.label.upper(),
                      fill=spec_label_color, font=spec_label_font)
            draw.text((col_right_x, y + sample_lh + label_to_value), val.upper(),
                      fill=spec_value_color, font=spec_value_font)

        y += row_content_h + row_gap

    # Center divider
    if max_rows > 0:
        draw.line([(divider_x, spec_top_y), (divider_x, y - row_gap)],
                  fill=spec_divider_color, width=2)

    # ── ROAST DATA STRIP (full-width dark bar) ─────────────────────────
    y = strip_top
    strip_bg = theme.color("strip_bg", "#000000")
    strip_label_color = theme.color("strip_label", "#555555")
    strip_value_color = theme.color("strip_value", "#ffffff")
    strip_divider_color = theme.color("strip_divider", "#333333")

    strip_label_font = _find_font(28, family=body_family)
    strip_value_font = _find_font(36, bold=True, family=heading_family)
    s_lh = _text_size(draw, "X", strip_label_font)[1]
    s_vh = _text_size(draw, "X", strip_value_font)[1]
    strip_pad_v = (strip_h - s_lh - 6 - s_vh) // 2  # center content vertically

    draw.rectangle([0, y, w_px, y + strip_h], fill=strip_bg)

    if strip:
        n_cols = len(strip)
        col_w = w_px // max(n_cols, 1)
        inner_pad = int(col_w * 0.12)  # consistent inner padding per column
        for ci, fs in enumerate(strip):
            cx = ci * col_w
            val = _resolve_field(data, fs, "\u2014")
            draw.text((cx + inner_pad, y + strip_pad_v), fs.label.upper(),
                      fill=strip_label_color, font=strip_label_font)
            draw.text((cx + inner_pad, y + strip_pad_v + s_lh + 6), val,
                      fill=strip_value_color, font=strip_value_font)
            if ci > 0:
                draw.line([(cx, y + 10), (cx, y + strip_h - 10)],
                          fill=strip_divider_color, width=2)

    y += strip_h

    # ── FOOTER (brand lockup left, QR right) ───────────────────────────
    footer_top = y
    footer_border_color = theme.color("footer_border", "#000000")
    draw.line([(0, footer_top), (w_px, footer_top)], fill=footer_border_color, width=2)

    footer_mid_y = footer_top + footer_h // 2

    # Ⓑ logo
    logo_border_color = theme.color("logo_border", "#000000")
    logo_b_color = theme.color("logo_b", "#000000")
    # Brand text (left-aligned with specs text)
    brand_name_color = theme.color("brand_name", "#000000")
    brand_tagline_color = theme.color("brand_tagline", "#888888")
    brand_x = int(text_pad * 0.5)  # tighter left edge than specs
    brand_font = _find_font(42, bold=True, family=heading_family)
    tagline_font = _find_font(28, bold=True, family=body_family)
    _, bnh = _text_size(draw, theme.brand_name.upper(), brand_font)
    _, tgh = _text_size(draw, theme.brand_tagline.upper(), tagline_font)
    brand_gap = 8
    total_text_h = bnh + tgh + brand_gap
    tt = footer_mid_y - total_text_h // 2
    draw.text((brand_x, tt), theme.brand_name.upper(),
              fill=brand_name_color, font=brand_font)
    draw.text((brand_x, tt + bnh + brand_gap), theme.brand_tagline.upper(),
              fill=brand_tagline_color, font=tagline_font)

    # Ⓑ logo (bottom right)
    if theme.logo_enabled:
        logo_border_color = theme.color("logo_border", "#000000")
        logo_b_color = theme.color("logo_b", "#000000")
        logo_radius = min(int(footer_h * 0.30), int(0.20 * dpi))
        logo_cx = w_px - text_pad - logo_radius
        if logo_radius > 10:
            _draw_logo_b(draw, logo_cx, footer_mid_y, logo_radius,
                         border_color=logo_border_color, fill_color=logo_b_color)

    # QR code moved to name block — footer just has brand lockup

    # ── CORNER MARKS (top corners only — bottom corners clash with footer) ──
    if theme.corner_marks:
        mark_color = theme.color("corner_marks", "#000000")
        mark_len = int(0.035 * w_px)
        inset = int(0.015 * w_px)
        mw = max(2, theme.border_width)
        corners = [
            (inset, inset, inset + mark_len, inset, inset, inset + mark_len),
            (w_px - inset, inset, w_px - inset - mark_len, inset,
             w_px - inset, inset + mark_len),
        ]
        for sx, sy, ex1, ey1, ex2, ey2 in corners:
            draw.line([(sx, sy), (ex1, ey1)], fill=mark_color, width=mw)
            draw.line([(sx, sy), (ex2, ey2)], fill=mark_color, width=mw)

    # ── LABEL BORDER (optional, e.g. for B&W style) ───────────────────
    label_border = theme.color("label_border", "")
    if label_border:
        draw.rectangle([0, 0, w_px - 1, h_px - 1],
                       outline=label_border, width=max(2, theme.border_width))

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(dpi, dpi))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Drop layout renderer (v13 — all black-on-white typography, no solid fills)
# ---------------------------------------------------------------------------

# Bundled assets directory (logos, etc.)
_ASSETS_DIR = Path(__file__).parent / "assets"


def _load_circle_b_logo(theme) -> Image.Image | None:
    """Load and prepare a Circle-B logo PNG for the drop layout.

    Returns an RGBA image cropped to the circular mark and converted to
    black-on-transparent, or None if the file is missing.
    """
    from PIL import Image

    # Determine logo path
    if theme.logo_path:
        logo_file = Path(theme.logo_path)
    else:
        version = 2 if theme.brand_variant == "hrs" else 1
        logo_file = _ASSETS_DIR / f"circle-b-v{version}.png"

    if not logo_file.exists():
        _logger.warning("Logo file not found: %s", logo_file)
        return None

    src = Image.open(logo_file).convert("RGBA")
    w, h = src.size
    cx, cy = w // 2, h // 2
    half = min(w, h) // 2 - 20
    return src.crop((cx - half, cy - half, cx + half, cy + half))


def _render_logo_bw(src: Image.Image, size: int) -> Image.Image:
    """Resize a logo and convert to black-on-transparent."""
    from PIL import Image

    resized = src.resize((size, size), Image.LANCZOS)
    gray = resized.convert("L")
    bw = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    pixels = gray.load()
    bw_pixels = bw.load()
    for py in range(size):
        for px in range(size):
            if pixels[px, py] < 160:
                bw_pixels[px, py] = (15, 15, 15, 255)
    return bw


def _render_drop(data: LabelData, theme) -> bytes:
    """Render the drop layout — v13 black-on-white typography design.

    Optimized for Zebra ZD-421 thermal transfer: no solid fills, all
    black (#0f0f0f) on white (#ffffff), horizontal rules as structure.
    """
    from PIL import Image, ImageDraw

    W = int(theme.width_inches * theme.dpi)
    H = int(theme.height_inches * theme.dpi)
    INK = (15, 15, 15)
    WHITE = (255, 255, 255)
    margin = 60
    right = W - margin

    img = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    # Font helpers using the existing _find_font infrastructure
    # Impact for headers/names, Arial Bold for values, Arial for labels
    def f_impact(size):
        return _find_font(size, bold=True, family="Impact")

    def f_bold(size):
        return _find_font(size, bold=True, family="Arial")

    def f_light(size):
        return _find_font(size, bold=False, family="Arial")

    def twidth(text, f):
        bbox = draw.textbbox((0, 0), text, font=f)
        return bbox[2] - bbox[0]

    def tbbox(text, f):
        return draw.textbbox((0, 0), text, font=f)

    # === HEADER ===
    rule1_y = 45
    draw.rectangle([(margin, rule1_y), (right, rule1_y + 3)], fill=INK)

    header_h = 80
    rule2_y = rule1_y + 3 + header_h

    f_drop = f_impact(56)
    raw_num = data.bond_number
    if raw_num is not None:
        drop_text = f"DROP #{raw_num:03d}"
    elif theme.brand_name:
        drop_text = "DROP #001"
    else:
        drop_text = "ROAST"
    bbox = tbbox(drop_text, f_drop)
    text_h = bbox[3] - bbox[1]
    text_ascent_offset = bbox[1]
    space_top = rule1_y + 3
    space_bottom = rule2_y
    drop_y = space_top + (space_bottom - space_top - text_h) // 2 - text_ascent_offset
    draw.text((margin, drop_y), drop_text, font=f_drop, fill=INK)

    # Roast name right-aligned in header
    if data.roast_name:
        f_rname = f_bold(32)
        rn_w = twidth(data.roast_name, f_rname)
        rn_bbox = tbbox(data.roast_name, f_rname)
        rn_h = rn_bbox[3] - rn_bbox[1]
        rn_ascent = rn_bbox[1]
        rn_y = space_top + (space_bottom - space_top - rn_h) // 2 - rn_ascent
        draw.text((right - rn_w, rn_y), data.roast_name, font=f_rname, fill=INK)

    draw.rectangle([(margin, rule2_y), (right, rule2_y + 2)], fill=INK)
    y = rule2_y + 22

    # === COFFEE NAME ===
    name_upper = data.coffee_name.upper()
    # Split into lines: use the theme's word-wrap or manual line breaks
    name_lines = _fit_text_lines(draw, name_upper, f_impact(118), right - margin)

    if len(name_lines) <= 2:
        hero_size = 118
        line_height = 126
    else:
        hero_size = 108
        line_height = 116

    f_hero = f_impact(hero_size)
    # Re-wrap at the actual hero size
    name_lines = _fit_text_lines(draw, name_upper, f_hero, right - margin)

    for line in name_lines:
        draw.text((margin, y), line, font=f_hero, fill=INK)
        y += line_height

    y += 16
    draw.rectangle([(margin, y), (right, y + 2)], fill=INK)
    y += 20

    # === DETAILS GRID (3 columns with vertical dividers) ===
    f_detail_label = f_light(20)
    divider_xs = (400, 770)
    divider_padding = 12
    column_gap = 30
    column_starts = (margin, *(x + column_gap for x in divider_xs))
    column_rights = (*(x - divider_padding for x in divider_xs), right)
    grid_top = y
    label_height = 26
    value_line_height = 28
    rows = (
        (("ORIGIN", data.origin), ("PROCESS", data.processing),
         ("WEIGHT", data.weight_display or "12OZ / 340G")),
        (("VARIETAL", data.varietal), ("ROAST", data.roast_level),
         ("ROASTED", data.roast_date)),
    )
    for row, single_line_height in zip(rows, (48, 50), strict=True):
        value_height = single_line_height
        for (label, value), x, cell_right in zip(
            row, column_starts, column_rights, strict=True,
        ):
            draw.text((x, y), label, font=f_detail_label, fill=INK)
            font, lines = _fit_cell_text(draw, (value or "").upper(), cell_right - x, f_bold)
            ink_bottom = 0
            for j, line in enumerate(lines):
                line_y = y + label_height + j * value_line_height
                draw.text((x, line_y), line, font=font, fill=INK)
                ink_bottom = max(ink_bottom, draw.textbbox((0, 0), line, font=font)[3]
                                 + j * value_line_height)
            value_height = max(value_height, ink_bottom + 16)
        y += label_height + value_height

    # Column dividers span both rows, including wrapped values.
    for x in divider_xs:
        draw.line([(x, grid_top), (x, y - 5)], fill=INK, width=1)

    draw.rectangle([(margin, y), (right, y + 2)], fill=INK)
    y += 10

    # === FOOTER ===
    footer_bottom = H - 46
    # Reserve the bottom brand lockup below the centered logo/QR group.
    brand_reserve = 110 if theme.brand_name else 0
    footer_height = footer_bottom - brand_reserve - y
    footer_center_y = y + footer_height // 2

    # Logo — load Circle-B PNG, crop + convert to black-on-transparent
    if theme.logo_enabled:
        logo_src = _load_circle_b_logo(theme)
        logo_size = min(footer_height - 20, 400)
        if logo_src is not None and logo_size > 20:
            logo_rendered = _render_logo_bw(logo_src, logo_size)
            logo_x = margin
            logo_y = y + (footer_height - logo_size) // 2
            img.paste(logo_rendered, (logo_x, logo_y), logo_rendered)
            footer_center_y = logo_y + logo_size // 2
        else:
            # Fallback: draw the vector circled-B
            logo_radius = min(footer_height - 20, 400) // 2
            if logo_radius > 20:
                _draw_logo_b(draw, margin + logo_radius, footer_center_y, logo_radius,
                             border_color="#0f0f0f", fill_color="#0f0f0f")

    # Center the complete QR + code + short URL group in the footer content zone.
    qr_size = min(240, footer_height - 82)
    if data.qr_url and theme.qr_enabled:
        qr_column_width = 280
        qr_cx = right - qr_column_width // 2
        qr_top = footer_center_y - (qr_size + 62) // 2
        _draw_qr(img, draw, data.qr_url,
                 qr_cx - qr_size // 2, qr_top, qr_size,
                 hint_text="", fg="black", bg="white")
        _draw_qr_text(draw, data, qr_cx, qr_top + qr_size + 6, qr_column_width)

    # Brand text + tagline — lower right (skip if empty)
    if theme.brand_name:
        f_brand = f_bold(48)
        f_tagline = f_light(18)
        f_hrs_tag = f_bold(18)

        brand_name = theme.brand_name.upper()
        brand_w = twidth(brand_name, f_brand)
        tagline = theme.brand_tagline.upper()
        # Letter-space the tagline like the reference
        spaced_tagline = "   ".join(tagline) if len(tagline) < 30 else tagline
        tagline_w = twidth(spaced_tagline, f_tagline)

        brand_y = footer_bottom - 78
        draw.text((right - brand_w, brand_y), brand_name, font=f_brand, fill=INK)
        draw.text((right - tagline_w, brand_y + 52), spaced_tagline, font=f_tagline, fill=INK)

        # HRS tag above brand name
        is_hrs = theme.brand_variant == "hrs"
        if is_hrs:
            hrs_text = "H R S"
            hrs_w = twidth(hrs_text, f_hrs_tag)
            draw.text((right - hrs_w, brand_y - 26), hrs_text, font=f_hrs_tag, fill=INK)
    elif data.flavor_notes or data.altitude:
        # No brand — use footer for extra coffee details
        detail_y = y + 16
        f_dlabel = f_light(20)
        f_dvalue = f_bold(32)
        if data.flavor_notes:
            draw.text((margin, detail_y), "TASTING NOTES", font=f_dlabel, fill=INK)
            detail_y += 26
            notes_upper = data.flavor_notes.upper()
            notes_lines = _fit_text_lines(draw, notes_upper, f_dvalue, right - margin)
            for line in notes_lines[:2]:
                draw.text((margin, detail_y), line, font=f_dvalue, fill=INK)
                detail_y += 40
            detail_y += 10
        extras = []
        if data.altitude:
            extras.append(("ALTITUDE", f"{data.altitude}M"))
        if data.drop_temp:
            extras.append(("DROP TEMP", f"{data.drop_temp:.1f}°C"))
        if data.dtr:
            extras.append(("DTR", f"{data.dtr:.1f}%"))
        if extras:
            col_width = (right - margin) // min(len(extras), 4)
            for i, (elabel, evalue) in enumerate(extras[:4]):
                ex = margin + i * col_width
                draw.text((ex, detail_y), elabel, font=f_dlabel, fill=INK)
                draw.text((ex, detail_y + 26), evalue, font=f_dvalue, fill=INK)

    # Bottom rule
    draw.rectangle([(margin, footer_bottom), (right, footer_bottom + 4)], fill=INK)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(theme.dpi, theme.dpi))
    return buf.getvalue()


def _draw_qr_placeholder(draw, cx: int, cy: int, size: int) -> None:
    """Draw a QR-like placeholder when no URL is available."""
    INK = (15, 15, 15)
    half = size // 2
    x1, y1 = cx - half, cy - half
    x2, y2 = cx + half, cy + half
    draw.rectangle([(x1, y1), (x2, y2)], outline=INK, width=2)
    cell = size // 9
    for row in range(9):
        for col in range(9):
            if (row < 3 and col < 3) or (row < 3 and col > 5) or (row > 5 and col < 3):
                px = x1 + col * cell + 3
                py = y1 + row * cell + 3
                draw.rectangle([(px, py), (px + cell - 3, py + cell - 3)], fill=INK)
            elif (row + col) % 3 == 0:
                px = x1 + col * cell + 4
                py = y1 + row * cell + 4
                draw.rectangle([(px, py), (px + cell - 5, py + cell - 5)], fill=INK)


def _rgba_to_rgb(rgba_str: str, bg_hex: str) -> str:
    """Approximate an rgba color on a solid background as an RGB hex string."""
    if not rgba_str.startswith("rgba("):
        return rgba_str
    try:
        inner = rgba_str[5:-1]
        parts = [p.strip() for p in inner.split(",")]
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        a = float(parts[3])

        # Parse background
        bg = bg_hex.lstrip("#")
        br, bg_g, bb = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)

        # Alpha composite
        out_r = int(r * a + br * (1 - a))
        out_g = int(g * a + bg_g * (1 - a))
        out_b = int(b * a + bb * (1 - a))
        return f"#{out_r:02x}{out_g:02x}{out_b:02x}"
    except Exception:
        return "#333333"


# ---------------------------------------------------------------------------
# Public API — dispatcher
# ---------------------------------------------------------------------------

# Layout name → renderer function
_RENDERERS = {
    "classic": _render_classic,
    "craft": _render_craft,
    "drop": _render_drop,
}


def build_preview_png(data: LabelData, theme=None) -> bytes:
    """Render a label PNG using the specified theme.

    Args:
        data: Label data to render.
        theme: A LabelTheme instance. If None, uses a minimal classic default.

    Returns:
        PNG image bytes.
    """
    if theme is None:
        # Backward-compatible: use a default classic theme
        from maillard.web.label_styles import LabelTheme
        theme = LabelTheme(id="classic", name="Classic", layout="classic")

    renderer = _RENDERERS.get(theme.layout, _render_classic)
    return renderer(data, theme)


# ---------------------------------------------------------------------------
# ZPL Graphic Conversion (PNG → ^GFA for printing)
# ---------------------------------------------------------------------------


def _png_to_zpl_graphic(png_bytes: bytes) -> str:
    """Convert a PNG to ZPL ^GFA (ASCII hex graphic) for the full label."""
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("1")
    width, height = img.size
    pixels = img.load()

    # ZPL graphic: each row is ceil(width/8) bytes
    row_bytes = (width + 7) // 8
    total_bytes = row_bytes * height

    # Build raw bitmap data and convert to hex row by row
    # In ^GFA: a set bit = black dot on the label
    hex_rows: list[str] = []
    for row in range(height):
        row_data = bytearray(row_bytes)
        for col in range(width):
            if pixels[col, row] == 0:  # black pixel in PIL "1" mode
                byte_idx = col // 8
                bit_idx = 7 - (col % 8)
                row_data[byte_idx] |= 1 << bit_idx
        hex_rows.append(row_data.hex().upper())

    hex_data = "\n".join(hex_rows)
    data_len = total_bytes  # length of hex-decoded data

    zpl_lines = [
        "^XA",
        f"^LL{height}",
        f"^PW{width}",
        "^FO0,0",
        f"^GFA,{data_len},{data_len},{row_bytes},",
        hex_data,
        "^FS",
        "^XZ",
    ]
    return "\n".join(zpl_lines)


def build_zpl(data: LabelData, theme=None) -> str:
    """Build ZPL by rendering the label as an image and converting to ^GFA graphic."""
    png_bytes = build_preview_png(data, theme=theme)
    return _png_to_zpl_graphic(png_bytes)


# ---------------------------------------------------------------------------
# TCP Sender
# ---------------------------------------------------------------------------


def send_to_printer(zpl: str, host: str, port: int = 9100) -> None:
    """Send ZPL to a Zebra printer over raw TCP (fallback path)."""
    _logger.info("Sending label to printer %s:%d (%d bytes)", host, port, len(zpl))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(TCP_TIMEOUT)
        sock.connect((host, port))
        sock.sendall(zpl.encode("utf-8"))
    _logger.info("Label sent successfully")


def print_via_cups(
    png_bytes: bytes,
    cups_name: str,
    darkness: int = 28,
    copies: int = 1,
    label_width: float = 4.0,
    label_height: float = 4.0,
    speed: int = 2,
) -> None:
    """Print a label PNG via CUPS (preferred path — best quality).

    The Zebra CUPS driver handles PNG-to-ZPL conversion with optimized
    dithering that produces sharper blacks than manual conversion.
    """
    media = f"Custom.{label_width}x{label_height}in"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        tmp_path = f.name

    try:
        cmd = [
            "lp", "-d", cups_name,
            "-n", str(copies),
            "-o", f"media={media}",
            "-o", "fit-to-page",
            "-o", "Resolution=300dpi",
            "-o", f"Darkness={darkness}",
            "-o", f"PrintRate={speed}",
            tmp_path,
        ]
        _logger.info("Printing via CUPS: %s (darkness=%d)", cups_name, darkness)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"lp failed: {result.stderr.strip()}")
        _logger.info("CUPS print submitted: %s", result.stdout.strip())
    finally:
        os.unlink(tmp_path)


def require_packed_weights(roast) -> None:
    """Every roast label print must pass this gate, including sync overrides."""
    from fastapi import HTTPException
    if roast.roasted_weight_grams is None or roast.bags_packed is None:
        raise HTTPException(409, "Record roasted weight and bags packed before printing labels")
