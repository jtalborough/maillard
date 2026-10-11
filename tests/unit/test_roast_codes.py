"""Roast codes (6-char Crockford base32) and weights."""

from __future__ import annotations

import pytest

from maillard.roasting.codes import compute_weight_loss, ensure_roast_code, regenerate_roast_code
from maillard.utils.naming import (
    CROCKFORD_ALPHABET,
    generate_roast_code,
    is_valid_roast_code,
)


def test_code_format():
    for _ in range(500):
        code = generate_roast_code()
        assert len(code) == 6
        assert is_valid_roast_code(code)
        assert not set(code) & set("ILOU")
    assert len(CROCKFORD_ALPHABET) == 32


@pytest.mark.parametrize("bad", [None, "", "K7Q2M", "K7Q2MXX", "k7q2mx", "K7Q2MI", "K7-2MX"])
def test_invalid_codes(bad):
    assert not is_valid_roast_code(bad)


@pytest.mark.parametrize(
    ("green", "roasted", "expected"),
    [(1200, 1020, 15.0), (1000, 862.5, 13.75), (None, 800, None), (1000, None, None),
     (1000, 1100, None), (0, 0, None)],
)
def test_weight_loss(green, roasted, expected):
    assert compute_weight_loss(green, roasted) == expected


@pytest.mark.asyncio
async def test_ensure_and_regenerate(mongo_db):  # noqa: ARG001
    from maillard.db.models import Roast

    roast = Roast()
    await roast.insert()
    code = await ensure_roast_code(roast)
    assert is_valid_roast_code(code)
    assert await ensure_roast_code(roast) == code  # idempotent
    new = await regenerate_roast_code(roast)
    assert new != code
    assert (await Roast.get(roast.id)).code == new
