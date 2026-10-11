"""Tests for the roast name generator."""
from maillard.utils.naming import generate_roast_name, ADJECTIVES, NOUNS_CORE


# Coffee flavor/tasting terms that should NOT appear in word lists
FLAVOR_WORDS = {
    "cherry", "honey", "plum", "fig", "lime", "lemon", "orange", "apple",
    "berry", "peach", "mango", "grape", "cocoa", "chocolate", "caramel",
    "vanilla", "cinnamon", "nutmeg", "clove", "ginger", "rose", "jasmine",
    "lavender", "mint", "sage", "basil", "sweet", "tart", "bitter", "sour",
    "fruity", "floral", "nutty", "smoky", "earthy", "spicy", "creamy",
    "bright", "crisp", "smooth", "rich", "ripe", "rosy", "silk",
}


def test_generate_returns_string():
    name = generate_roast_name()
    assert isinstance(name, str)


def test_format_adjective_noun_roast():
    name = generate_roast_name()
    parts = name.split("-")
    assert len(parts) == 3
    assert parts[2] == "roast"


def test_words_from_lists():
    for _ in range(20):
        name = generate_roast_name()
        adj, core, suffix = name.split("-")
        assert adj in ADJECTIVES
        assert core in NOUNS_CORE
        assert suffix == "roast"


def test_uniqueness():
    names = {generate_roast_name() for _ in range(50)}
    assert len(names) > 10  # should be mostly unique


def test_no_flavor_words_in_core_nouns():
    """Core nouns should not contain coffee flavor/tasting descriptors."""
    overlap = set(NOUNS_CORE) & FLAVOR_WORDS
    assert not overlap, f"NOUNS_CORE contains flavor words: {overlap}"


def test_no_flavor_words_in_adjectives():
    """Adjectives should not contain coffee flavor/tasting descriptors."""
    overlap = set(ADJECTIVES) & FLAVOR_WORDS
    assert not overlap, f"ADJECTIVES contains flavor words: {overlap}"
