"""Three-word roast name generator.

Generates memorable hyphenated names in adjective-noun-roast format
(e.g. 'steady-anvil-roast'). Word lists avoid coffee flavor/tasting
terms to prevent confusion with cupping notes on bag labels.
"""

import random

ADJECTIVES = [
    "bold", "brave", "calm", "clear", "cold", "dark", "deep", "dry",
    "even", "fair", "fast", "firm", "flat", "full", "grand", "hard",
    "high", "keen", "last", "lean", "live", "long", "loud", "near",
    "neat", "new", "north", "odd", "old", "pale", "plain", "prime",
    "quick", "rare", "raw", "red", "safe", "sharp", "shy", "slow",
    "small", "south", "steady", "still", "strong", "sure", "tall",
    "thin", "true", "twin", "vast", "warm", "west", "wide", "wild",
    "wise", "young",
]

NOUNS_CORE = [
    "anvil", "arch", "axle", "band", "bark", "barn", "beam", "bell",
    "birch", "blade", "bolt", "bone", "brass", "brick", "brook", "cairn",
    "cedar", "chain", "chalk", "clay", "coal", "coil", "copper", "cord",
    "crane", "drum", "dust", "elm", "ember", "fence", "flint", "forge",
    "frame", "gear", "glass", "grain", "grip", "hammer", "hatch", "helm",
    "hinge", "hull", "iron", "kiln", "latch", "ledge", "lever", "lock",
    "mast", "nail", "oak", "ore", "pine", "plank", "press", "rail",
    "rivet", "rope", "shaft", "slate", "spoke", "steel", "stone", "strut",
    "timber", "torch", "vice", "wedge", "wheel", "wire",
]

def generate_roast_name() -> str:
    """Generate a three-word hyphenated roast name (adjective-noun-roast)."""
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS_CORE)}-roast"


# Roast codes: 6 chars of Crockford base32 (no I, L, O, U) -> ~1.07e9 codes.
# Printed on labels and used in https://roastery.example.com/r/<code>.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ROAST_CODE_LENGTH = 6


def generate_roast_code(length: int = ROAST_CODE_LENGTH) -> str:
    """Random roast code, e.g. 'K7Q2MX' (cryptographic RNG, not sequential)."""
    import secrets

    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(length))


def is_valid_roast_code(code: str | None) -> bool:
    """True if ``code`` is a well-formed roast code."""
    return (
        code is not None
        and len(code) == ROAST_CODE_LENGTH
        and all(c in CROCKFORD_ALPHABET for c in code)
    )
