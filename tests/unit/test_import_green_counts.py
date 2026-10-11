"""Count parsing/matching and read-only preview vs explicit idempotent application."""

import hashlib
import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from scripts.import_green_counts import Count, compute_plan, main, match_coffee, parse_counts, run

from maillard.db.models import Coffee, GreenLot, InventoryMove, InventoryOperation
from maillard.inventory import service as inv

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.mark.parametrize("format", ["csv", "json", "rows"])
def test_parsing(tmp_path, format):
    path = tmp_path / ("counts.csv" if format == "csv" else "counts.json")
    inputs = {
        "csv": "coffee,grams,supplier,cost_per_kg,received_at\nUraga,1200,Test,15,2026-10-03\n",
        "json": '{"Uraga":{"grams":1200,"supplier":"Test","cost_per_kg":15,"received_at":"2026-10-03"}}',
        "rows": '[{"coffee":"Uraga","grams":1200,"supplier":"Test","cost_per_kg":15,"received_at":"2026-10-03"}]',
    }
    path.write_text(inputs[format])
    counts = parse_counts(path)
    assert counts[0].coffee == "Uraga" and counts[0].grams == 1200
    assert counts[0].cost_per_kg == 15 and counts[0].supplier == "Test"
    assert counts[0].received_at == "2026-10-03T00:00:00+00:00"
    path = tmp_path / "simple.json"
    path.write_text('{"Uraga": 1200}')
    assert parse_counts(path)[0].grams == 1200


@pytest.mark.parametrize(
    "content",
    [
        '{"Uraga": -1}',
        '{"Uraga": "nan"}',
        '{"Uraga": "inf"}',
        '{"Uraga":1,"uraga":2}',
        '[{"coffee":"","grams":1}]',
        '{"Uraga":{"grams":1,"cost_per_kg":-2}}',
    ],
)
def test_invalid_counts(tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        parse_counts(path)


def test_match_priority_and_ambiguity():
    coffees = [
        Coffee(name="Uraga", origin="E"),
        Coffee(name="Uraga Natural", origin="E"),
        Coffee(name="Wush Wush", origin="C"),
    ]
    assert match_coffee("Uraga", coffees) is coffees[0]
    assert match_coffee("uraga", coffees) is coffees[0]
    assert match_coffee("WUSH", coffees) is coffees[2]
    with pytest.raises(ValueError, match="Ambiguous"):
        match_coffee("Urag", coffees)
    with pytest.raises(ValueError, match="Unmatched"):
        match_coffee("Kenya", coffees)


def test_plan():
    count = Count("Uraga", 700)
    assert compute_plan(count, "c", 1000, True)["delta"] == -300
    assert compute_plan(count, "c", 0, False)["action"] == "create zero receipt + adjustment"
    assert compute_plan(count, "c", 400, True, True)["delta"] == 0


@pytest.mark.parametrize("existing", [True, False])
async def test_dry_run_apply_and_reapply(tmp_path, capsys, existing):
    coffee = await Coffee(name="Uraga", origin="Ethiopia").insert()
    if existing:
        await inv.receive_lot(coffee.id, 1000)
    path = tmp_path / "counts.csv"
    path.write_text("coffee,grams\nUraga,700\n")
    original_moves = await InventoryMove.count()
    original_lots = await GreenLot.count()
    with patch("scripts.import_green_counts.close_db", AsyncMock()):
        assert await run(path) == 0
        assert "DRY RUN" in capsys.readouterr().out
        assert await InventoryMove.count() == original_moves
        assert await GreenLot.count() == original_lots
        assert await run(path, apply=True) == 0
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 700
        new_moves = await InventoryMove.count()
        # Simulate a roast after the count; replaying the file must preserve the new balance.
        lot = (await GreenLot.find_all().to_list())[-1]
        await inv.adjust(lot.id, -100, note="used since count")
        assert await run(path, apply=True) == 0
        assert "no-op (count already applied)" in capsys.readouterr().out
        assert await InventoryMove.count() == new_moves + 1
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 600
        assert await run(path, apply=True, count_id="fresh-count") == 0
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 700


async def test_unmatched_default_writes_nothing(tmp_path):
    await Coffee(name="Uraga", origin="E").insert()
    path = tmp_path / "counts.json"
    path.write_text('{"Uraga":1000,"Unknown":500}')
    with patch("scripts.import_green_counts.close_db", AsyncMock()):
        assert await run(path, apply=True) == 1
        assert await GreenLot.count() == await InventoryMove.count() == 0
        assert await run(path, apply=True, skip_unmatched=True) == 0
        assert await GreenLot.count() == await InventoryMove.count() == 1


async def test_two_aliases_cannot_apply_two_counts(tmp_path):
    await Coffee(name="Ethiopia Uraga", origin="E").insert()
    path = tmp_path / "counts.json"
    path.write_text('{"Ethiopia Uraga":1000,"Uraga":500}')
    with patch("scripts.import_green_counts.close_db", AsyncMock()):
        assert await run(path, apply=True) == 1
        assert await GreenLot.count() == 0


@pytest.mark.parametrize(
    "format,content",
    [
        (None, b"\xef\xbb\xbf# pasted count\r\nUraga: 0\r\n"),
        ("txt", b"Uraga: 0\n"),
        ("csv", b"coffee,grams\nUraga,0\n"),
        ("json", b'{"Uraga": 0}'),
    ],
)
def test_stdin_formats_preserve_zero(format, content):
    with patch("scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(content))):
        assert parse_counts(Path("-"), format=format) == [Count("Uraga", 0)]


async def test_stdin_exact_byte_id_and_retry_after_roast(capsys):
    coffee = await Coffee(name="Uraga", origin="Ethiopia").insert()
    content = b"\xef\xbb\xbfUraga: 700\r\n"
    digest = hashlib.sha256(content).hexdigest()
    with patch("scripts.import_green_counts.close_db", AsyncMock()):
        with patch("scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(content))):
            assert await run(Path("-")) == 0
        assert await InventoryMove.count() == 0
        with patch("scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(content))):
            assert await run(Path("-"), apply=True) == 0
        assert await InventoryOperation.get(f"count:{digest}:{coffee.id}") is not None
        lot = (await GreenLot.find_all().to_list())[0]
        await inv.adjust(lot.id, -100, note="used after pasted count")
        with patch("scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(content))):
            assert await run(Path("-"), apply=True) == 0
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 600
        assert "no-op (count already applied)" in capsys.readouterr().out
        with patch("scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(content))):
            assert await run(Path("-"), apply=True, count_id="new-pasted-count") == 0
        assert (await inv.coffee_summary(coffee.id))["remaining"] == 700


def test_cli_stdin_format_override_and_invalid_input(tmp_path, capsys):
    snapshot = tmp_path / "coffees.json"
    snapshot.write_text('[{"id":"c","name":"Uraga"}]')
    with (
        patch(
            "sys.argv",
            ["import_green_counts.py", "-", "--format", "json", "--coffees-json", str(snapshot)],
        ),
        patch(
            "scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"Uraga":10}'))
        ),
        patch("scripts.import_green_counts.init_db", AsyncMock()) as init,
    ):
        assert main() == 0
        init.assert_not_called()
    assert "DRY RUN" in capsys.readouterr().out
    with (
        patch("sys.argv", ["import_green_counts.py", "-"]),
        patch(
            "scripts.import_green_counts.sys.stdin", io.TextIOWrapper(io.BytesIO(b"Uraga: -1\n"))
        ),
        patch("scripts.import_green_counts.init_db", AsyncMock()) as init,
    ):
        assert main() == 1
        init.assert_not_called()


def test_file_format_override(tmp_path):
    path = tmp_path / "counts.data"
    path.write_text("Uraga: 700\n")
    assert parse_counts(path, format="txt") == [Count("Uraga", 700)]
