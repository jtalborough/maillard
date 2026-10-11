"""Tests for cupping aggregation functions."""

from unittest.mock import MagicMock

from maillard.db.aggregation import (
    CuppingAggregation,
    aggregate_cuppings,
    dimension_averages,
    ranked_notes,
)


def _mock_cupping(**kwargs):
    """Create a mock Cupping ORM object."""
    defaults = {
        "aroma": None,
        "acidity": None,
        "sweetness": None,
        "body": None,
        "aftertaste": None,
        "overall": None,
        "flavor_notes": None,
        "defects": None,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


# =============================================================================
# aggregate_cuppings — empty
# =============================================================================


class TestAggregateEmpty:
    def test_empty_list(self):
        result = aggregate_cuppings([])
        assert result.count == 0
        assert result.avg_total is None
        assert result.dimension_averages == []
        assert result.ranked_flavor_notes == []
        assert result.ranked_defects == []

    def test_all_none_scores(self):
        c = _mock_cupping()  # all scores None
        result = aggregate_cuppings([c])
        assert result.count == 1
        assert result.avg_total is None
        assert result.dimension_averages == []


# =============================================================================
# aggregate_cuppings — single cupping
# =============================================================================


class TestAggregateSingle:
    def test_full_scores(self):
        c = _mock_cupping(
            aroma=8.0, acidity=7.5, sweetness=8.5,
            body=7.0, aftertaste=7.5, overall=8.0,
        )
        result = aggregate_cuppings([c])
        assert result.count == 1
        assert result.avg_total == 46.5
        assert len(result.dimension_averages) == 6
        # Each dimension has exactly 1 value, so average == value
        dims = {d.dimension: d.average for d in result.dimension_averages}
        assert dims["aroma"] == 8.0
        assert dims["body"] == 7.0

    def test_partial_scores(self):
        c = _mock_cupping(aroma=8.0, acidity=7.5)  # only 2 scores
        result = aggregate_cuppings([c])
        assert result.count == 1
        assert result.avg_total == 15.5
        assert len(result.dimension_averages) == 2

    def test_with_flavor_notes(self):
        c = _mock_cupping(
            aroma=8.0,
            flavor_notes=(["citrus", "floral", "honey"]),
        )
        result = aggregate_cuppings([c])
        assert result.ranked_flavor_notes == ["citrus", "floral", "honey"]

    def test_with_defects(self):
        c = _mock_cupping(
            aroma=7.0,
            defects=(["baked", "grassy"]),
        )
        result = aggregate_cuppings([c])
        assert result.ranked_defects == ["baked", "grassy"]


# =============================================================================
# aggregate_cuppings — multiple cuppings
# =============================================================================


class TestAggregateMultiple:
    def test_averages_scores(self):
        c1 = _mock_cupping(aroma=8.0, acidity=7.0, sweetness=8.0, body=7.0, aftertaste=7.0, overall=7.5)
        c2 = _mock_cupping(aroma=9.0, acidity=8.0, sweetness=8.5, body=7.5, aftertaste=8.0, overall=8.5)
        c3 = _mock_cupping(aroma=7.0, acidity=7.5, sweetness=7.5, body=6.5, aftertaste=7.5, overall=7.0)
        result = aggregate_cuppings([c1, c2, c3])
        assert result.count == 3
        # c1 total=44.5, c2 total=49.5, c3 total=43.0 → avg = 45.67
        assert result.avg_total == 45.67
        dims = {d.dimension: d for d in result.dimension_averages}
        assert dims["aroma"].average == 8.0  # (8+9+7)/3
        assert dims["aroma"].count == 3

    def test_partial_scores_across_cuppings(self):
        """Different cuppings may have different dimensions scored."""
        c1 = _mock_cupping(aroma=8.0, acidity=7.0)  # total=15
        c2 = _mock_cupping(aroma=9.0, body=7.5)  # total=16.5
        result = aggregate_cuppings([c1, c2])
        assert result.count == 2
        assert result.avg_total == 15.75
        dims = {d.dimension: d for d in result.dimension_averages}
        assert dims["aroma"].average == 8.5  # (8+9)/2
        assert dims["aroma"].count == 2
        assert dims["acidity"].average == 7.0  # only c1
        assert dims["acidity"].count == 1
        assert dims["body"].average == 7.5  # only c2
        assert dims["body"].count == 1

    def test_frequency_ranked_flavor_notes(self):
        c1 = _mock_cupping(aroma=8.0, flavor_notes=(["citrus", "floral"]))
        c2 = _mock_cupping(aroma=8.0, flavor_notes=(["citrus", "honey"]))
        c3 = _mock_cupping(aroma=8.0, flavor_notes=(["citrus", "floral", "chocolate"]))
        result = aggregate_cuppings([c1, c2, c3])
        # citrus: 3, floral: 2, chocolate: 1, honey: 1
        # alphabetical tiebreak for chocolate vs honey
        assert result.ranked_flavor_notes == ["citrus", "floral", "chocolate", "honey"]

    def test_mixed_scored_and_unscored(self):
        c1 = _mock_cupping(aroma=8.0, overall=8.0)  # total=16
        c2 = _mock_cupping()  # all None, total=None
        result = aggregate_cuppings([c1, c2])
        assert result.count == 2
        # avg_total is mean of non-None totals only
        assert result.avg_total == 16.0


# =============================================================================
# dimension_averages
# =============================================================================


class TestDimensionAverages:
    def test_empty(self):
        assert dimension_averages([]) == []

    def test_skips_none(self):
        c1 = _mock_cupping(aroma=8.0)
        c2 = _mock_cupping(aroma=None)
        result = dimension_averages([c1, c2])
        assert len(result) == 1
        assert result[0].dimension == "aroma"
        assert result[0].average == 8.0
        assert result[0].count == 1


# =============================================================================
# ranked_notes
# =============================================================================


class TestRankedNotes:
    def test_empty(self):
        assert ranked_notes([]) == []

    def test_none_flavor_notes(self):
        c = _mock_cupping(flavor_notes=None)
        assert ranked_notes([c]) == []

    def test_invalid_json(self):
        c = _mock_cupping(flavor_notes="not json")
        assert ranked_notes([c]) == []

    def test_json_not_a_list(self):
        c = _mock_cupping(flavor_notes=({"note": "citrus"}))
        assert ranked_notes([c]) == []

    def test_case_normalization(self):
        c1 = _mock_cupping(flavor_notes=(["Citrus"]))
        c2 = _mock_cupping(flavor_notes=(["citrus"]))
        result = ranked_notes([c1, c2])
        assert result == ["citrus"]  # merged as lowercase

    def test_alphabetical_tiebreak(self):
        c1 = _mock_cupping(flavor_notes=(["honey"]))
        c2 = _mock_cupping(flavor_notes=(["chocolate"]))
        result = ranked_notes([c1, c2])
        # Both count=1, alphabetical: chocolate < honey
        assert result == ["chocolate", "honey"]

    def test_defects_attr(self):
        c = _mock_cupping(defects=(["baked", "grassy"]))
        result = ranked_notes([c], attr="defects")
        assert result == ["baked", "grassy"]

    def test_strips_whitespace(self):
        c = _mock_cupping(flavor_notes=(["  citrus  ", "floral"]))
        result = ranked_notes([c])
        assert result == ["citrus", "floral"]

    def test_skips_empty_strings(self):
        c = _mock_cupping(flavor_notes=(["", "citrus", "  "]))
        result = ranked_notes([c])
        assert result == ["citrus"]
