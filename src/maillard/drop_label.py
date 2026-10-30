"""Letter series for bag labels, kept separate from the numeric shop drop."""

from __future__ import annotations

import re
from typing import Any

_SERIES = re.compile(r"^[A-Z]{1,4}$")
_LABEL = re.compile(r"^([A-Z]{0,4})([0-9]+)$")
_LETTERS_THEN_DIGITS = re.compile(r"^[A-Z]*[0-9]+$")

SERIES_ERROR = "Drop letters must be 1-4 characters A-Z"
LABEL_ERROR = "Enter a drop like 004 or RB001"
NUMBER_ERROR = "Drop number must be from 1 to 999999"
_MAX_NUMBER = 999_999


def normalize_drop_series(value: object) -> str | None:
    """Return an uppercase A-Z series, or None when the series is blank."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(SERIES_ERROR)
    text = value.strip().upper()
    if not text:
        return None
    if _SERIES.fullmatch(text) is None:
        raise ValueError(SERIES_ERROR)
    return text


def canonical_series(value: object) -> str | None:
    """Series used on labels and in sync. Invalid stored values are ignored."""
    try:
        return normalize_drop_series(value)
    except ValueError:
        return None


def parse_drop_label(value: object) -> tuple[str | None, int | None]:
    """Parse a drop entry into (series, number). Blank clears both."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(LABEL_ERROR)
    if isinstance(value, int):
        if value < 1 or value > _MAX_NUMBER:
            raise ValueError(NUMBER_ERROR)
        return None, value
    text = value.strip().upper()
    if not text:
        return None, None
    if _LETTERS_THEN_DIGITS.fullmatch(text) is None:
        raise ValueError(LABEL_ERROR)
    match = _LABEL.fullmatch(text)
    if match is None:
        raise ValueError(SERIES_ERROR)
    letters, digits = match.group(1), match.group(2)
    if len(digits) > 6:
        raise ValueError(NUMBER_ERROR)
    number = int(digits)
    if number < 1:
        raise ValueError(NUMBER_ERROR)
    return (letters or None), number


def apply_drop_fields(body: dict[str, Any]) -> None:
    """Map a combined drop_label onto bond_number and drop_series."""
    if "drop_label" in body:
        series, number = parse_drop_label(body.pop("drop_label"))
        body["drop_series"] = series
        body["bond_number"] = number
    if "drop_series" in body:
        body["drop_series"] = normalize_drop_series(body["drop_series"])


def format_drop_code(number: int | None, series: str | None = None) -> str | None:
    """Zero-padded drop code, with a letter series when one is set."""
    if number is None:
        return None
    prefix = canonical_series(series) or ""
    return f"{prefix}{number:03d}"


def format_drop_heading(
    number: int | None, series: str | None = None, *, branded: bool = False
) -> str:
    """Header line for the drop layout. Plain numbers stay DROP #004."""
    code = format_drop_code(number, series)
    if code is None:
        return "DROP #001" if branded else "ROAST"
    return f"DROP #{code}"
