"""Roast code assignment and weight helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from maillard.utils.naming import generate_roast_code

if TYPE_CHECKING:
    from maillard.db.models import Roast

_MAX_ATTEMPTS = 20


async def _unused_code() -> str:
    from maillard.db.models import Roast

    for _ in range(_MAX_ATTEMPTS):
        code = generate_roast_code()
        if await Roast.find_one(Roast.code == code) is None:
            return code
    raise RuntimeError("Could not find an unused roast code")


async def ensure_roast_code(roast: Roast, *, save: bool = True) -> str:
    """Return the roast's code, assigning a fresh unique one if missing."""
    if roast.code:
        return roast.code
    roast.code = await _unused_code()
    if save:
        await roast.set({"code": roast.code})
    return roast.code


async def regenerate_roast_code(roast: Roast) -> str:
    """Assign a new code (e.g. after the site reports a code collision)."""
    roast.code = await _unused_code()
    await roast.set({"code": roast.code})
    return roast.code


def compute_weight_loss(green: float | None, roasted: float | None) -> float | None:
    """Weight loss % from green and roasted weights (None if not computable)."""
    if not green or roasted is None or green <= 0 or roasted < 0 or roasted > green:
        return None
    return round((green - roasted) / green * 100, 2)


__all__ = [
    "compute_weight_loss",
    "ensure_roast_code",
    "regenerate_roast_code",
]
