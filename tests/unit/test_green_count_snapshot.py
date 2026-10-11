"""Synthetic count fixture, whole-word matching, and truly offline preview."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from scripts.import_green_counts import (
    CoffeeSnapshot,
    compute_plan,
    load_coffees_snapshot,
    main,
    match_coffee,
    parse_counts,
    run,
)

from maillard.db.models import Coffee, GreenLot, InventoryMove, InventoryOperation
from maillard.inventory import service as inv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COUNT_FILE = FIXTURES / "green_count_2026-10-03.txt"
COFFEES_FILE = FIXTURES / "maillard_coffees_2026-10-03.json"
EXPECTED = {
    "Flores Turelelo": ("Flores Anaerobic Honey Process Turelelo", 640),
    "Sumatra Gayo": ("Sumatra Aged Gayo 5-Year Vintage", 1500),
    "Wush Wush": ("Colombia Edward Sandoval Wush Wush", 1120),
    "Gera Nano Challa": ("Ethiopia Organic Gera Nano Challa", 410),
    "Guatemala Gesha": ("Guatemala Acatenango Gesha AA", 2200),
    "Uraga Yabitu Koba": ("Ethiopia Dry Process Uraga Yabitu Koba", 1360),
    "Vereda El Paramo": ("Colombia Honey Vereda El Paramo", 300),
    "Vereda San Francisco": ("Colombia Honey Vereda San Francisco", 1985),
    "Yemen Sharqui Haraz": ("Yemen Sharqui Haraz", 0),
    "Peru Gesha": ("Peru Organic Guardian del Bosque Gesha", 0),
    "Yirgacheffe": ("Ethiopia Yirgacheffe", 0),
    "Kenya": ("Kenya Moringa Nadira AA", 0),
}


def test_count_fixture_parses_and_all_names_match_uniquely():
    counts = parse_counts(COUNT_FILE)
    coffees = load_coffees_snapshot(COFFEES_FILE)
    assert len(counts) == len(coffees) == len(EXPECTED) == 12
    assert sum(count.grams == 0 for count in counts) == 4
    assert sum(count.grams for count in counts) == 9515
    seen = set()
    for count in counts:
        coffee = match_coffee(count.coffee, coffees)
        expected_name, grams = EXPECTED[count.coffee]
        assert (coffee.name, count.grams) == (expected_name, grams)
        plan = compute_plan(count, coffee.id, current=0, has_lots=False)
        assert plan["delta"] == grams
        assert plan["action"] == "create zero receipt + adjustment"
        seen.add(coffee.id)
    assert seen == {coffee.id for coffee in coffees}


@pytest.mark.parametrize(
    "content,expected",
    [
        (
            "# comment\n\n   # indented comment\n Coffee : 0 \nOther: 10.5\n",
            [("Coffee", 0), ("Other", 10.5)],
        ),
        ("\ufeffCoffee: 0\r\n", [("Coffee", 0)]),
    ],
)
def test_text_comments_whitespace_and_zeros(tmp_path, content, expected):
    path = tmp_path / "count.TXT"
    path.write_text(content)
    assert [(count.coffee, count.grams) for count in parse_counts(path)] == expected


@pytest.mark.parametrize(
    "content",
    [
        "Coffee 100\n",
        "Coffee:\n",
        "Coffee: invalid\n",
        ": 100\n",
        "Coffee: -1\n",
        "Coffee: nan\n",
        "Coffee: inf\n",
        "Coffee: 1\ncoffee: 2\n",
    ],
)
def test_invalid_text_counts(tmp_path, content):
    path = tmp_path / "counts.txt"
    path.write_text(content)
    with pytest.raises(ValueError):
        parse_counts(path)


def test_matching_priority_before_tokens_and_whole_word_boundaries():
    coffees = [
        CoffeeSnapshot(id="1", name="Guatemala Gesha"),
        CoffeeSnapshot(id="2", name="Guatemala Acatenango AA Gesha"),
        CoffeeSnapshot(id="3", name="Guatemala Gesha AA"),
        CoffeeSnapshot(id="4", name="Sumatra Aged Gayo 5-Year Vintage"),
        CoffeeSnapshot(id="5", name="Kenya Moringa Nadira AA"),
    ]
    assert match_coffee("Guatemala Gesha", coffees) is coffees[0]
    assert match_coffee("guatemala gesha", coffees) is coffees[0]
    assert match_coffee("Gesha AA", coffees) is coffees[2]  # substring precedes token fallback
    assert match_coffee("GAYO sumatra", coffees) is coffees[3]
    assert match_coffee("Sumatra 5 Year", coffees) is coffees[3]
    with pytest.raises(ValueError, match="Ambiguous"):
        match_coffee("Gesha Guatemala", coffees)
    with pytest.raises(ValueError, match="Unmatched"):
        match_coffee("Ken Nadira", coffees)  # ken is not a whole word in Kenya
    with pytest.raises(ValueError, match="Unmatched"):
        match_coffee("Guatemala Ges", coffees[1:2])


async def test_offline_preview_has_no_db_calls(capsys):
    with (
        patch(
            "scripts.import_green_counts.init_db", AsyncMock(side_effect=AssertionError("DB init"))
        ) as init,
        patch(
            "scripts.import_green_counts.close_db",
            AsyncMock(side_effect=AssertionError("DB close")),
        ) as close,
        patch(
            "scripts.import_green_counts.coffee_summary",
            AsyncMock(side_effect=AssertionError("DB read")),
        ),
        patch(
            "scripts.import_green_counts.physical_count",
            AsyncMock(side_effect=AssertionError("DB write")),
        ),
        patch.object(Coffee, "find_all", side_effect=AssertionError("Beanie query")),
        patch.object(
            InventoryOperation, "get", AsyncMock(side_effect=AssertionError("Journal query"))
        ),
    ):
        assert await run(COUNT_FILE, coffees_json=COFFEES_FILE) == 0
        init.assert_not_awaited()
        close.assert_not_awaited()
    output = capsys.readouterr().out
    assert "OFFLINE SNAPSHOT" in output and "DRY RUN" in output
    assert "WARNING" not in output and "Unmatched" not in output
    rows = [line.split("\t") for line in output.splitlines() if "\t" in line]
    assert rows[0] == [
        "coffee",
        "matched coffee",
        "legacy (g)",
        "current",
        "counted",
        "delta",
        "action",
    ]
    assert len(rows[1:]) == 12
    for row in rows[1:]:
        expected_name, grams = EXPECTED[row[0]]
        assert row[1] == expected_name
        assert float(row[3]) == 0
        assert float(row[4]) == float(row[5]) == grams
    assert rows[1][2] == "1814"
    assert next(row[2] for row in rows if row[0] == "Kenya") == "unknown"


@pytest.mark.parametrize("apply", [False, True])
async def test_offline_apply_refusal(capsys, apply):
    with patch(
        "scripts.import_green_counts.init_db", AsyncMock(side_effect=AssertionError("DB init"))
    ):
        assert await run(COUNT_FILE, coffees_json=COFFEES_FILE, apply=apply) == (1 if apply else 0)
    if apply:
        assert "--apply is refused" in capsys.readouterr().out


def test_offline_cli_main_without_beanie(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["import_green_counts.py", str(COUNT_FILE), "--coffees-json", str(COFFEES_FILE)]
    )
    with patch(
        "scripts.import_green_counts.init_db", AsyncMock(side_effect=AssertionError("DB init"))
    ):
        assert main() == 0
    assert "DRY RUN" in capsys.readouterr().out


@pytest.mark.parametrize("skip_unmatched", [False, True])
async def test_offline_reports_ambiguities_and_omissions(tmp_path, capsys, skip_unmatched):
    count_file = tmp_path / "counts.txt"
    count_file.write_text("Flores Turelelo: 791\nGuatemala Gesha: 0\nUnknown: 1\n")
    snapshot = json.loads(COFFEES_FILE.read_text())
    snapshot.append({"id": "extra-gesha", "name": "Guatemala Antigua Gesha", "quantity_grams": 10})
    snapshot_file = tmp_path / "coffees.json"
    snapshot_file.write_text(json.dumps(snapshot))
    assert await run(count_file, coffees_json=snapshot_file, skip_unmatched=skip_unmatched) == (
        0 if skip_unmatched else 1
    )
    output = capsys.readouterr().out
    assert "Ambiguous coffee 'Guatemala Gesha'" in output
    assert "Unmatched coffee: 'Unknown'" in output
    assert "WARNING: Coffee not in count file; unchanged: Kenya Moringa Nadira AA" in output
    assert "unchanged: Flores" not in output


@pytest.mark.parametrize(
    "data",
    [
        {},
        [{"name": "Coffee"}],
        [{"id": "1"}],
        [{"id": "1", "name": "A"}, {"id": "1", "name": "B"}],
        [{"id": "1", "name": "A", "quantity_grams": "nan"}],
    ],
)
def test_invalid_snapshot(tmp_path, data):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_coffees_snapshot(path)


@pytest.mark.usefixtures("mongo_db")
async def test_fixture_apply_then_replay_is_noop_and_zeros_are_recorded(capsys):
    coffees = load_coffees_snapshot(COFFEES_FILE)
    for coffee in coffees:
        await Coffee(
            id=coffee.id, name=coffee.name, origin="fixture", quantity_grams=coffee.quantity_grams
        ).insert()
    # A coffee omitted from the count warns and retains its own balance/ledger.
    omitted = await Coffee(name="Costa Rica omitted", origin="C", quantity_grams=900).insert()
    untouched_lot = await inv.receive_lot(omitted.id, 321)
    before_omitted = await inv.coffee_summary(omitted.id)
    counts = parse_counts(COUNT_FILE)
    with patch("scripts.import_green_counts.close_db", AsyncMock()):
        assert await run(COUNT_FILE) == 0
        assert await GreenLot.count() == 1 and await InventoryMove.count() == 1
        assert await run(COUNT_FILE, apply=True) == 0
        assert "Coffee not in count file; unchanged: Costa Rica omitted" in capsys.readouterr().out
        assert await inv.coffee_summary(omitted.id) == before_omitted
        assert (await GreenLot.get(untouched_lot.id)).grams_remaining == 321
        for count in counts:
            coffee = match_coffee(count.coffee, coffees)
            lot = await GreenLot.find_one({"coffee_id": coffee.id})
            move = await InventoryMove.find_one({"coffee_id": coffee.id})
            assert lot.grams_in == 0 and lot.grams_remaining == count.grams
            assert move.kind == "adjusted" and move.grams == count.grams
            assert move.note.startswith("physical count ")
            assert (await Coffee.get(coffee.id)).quantity_grams == coffee.quantity_grams
        assert await GreenLot.count() == 13 and await InventoryMove.count() == 13
        assert await InventoryOperation.count() == 13
        await run(COUNT_FILE, apply=True)
        assert capsys.readouterr().out.count("no-op (count already applied)") == 12
        assert await GreenLot.count() == 13 and await InventoryMove.count() == 13
        assert await InventoryOperation.count() == 13
        assert sum(lot.grams_remaining == 0 for lot in await GreenLot.find_all().to_list()) == 4
