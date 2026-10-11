"""Phase mapping and conservative historical repair."""

import pytest

from maillard.roasting.events import derive_roast_event_fields, missing_roast_event_fields


@pytest.mark.parametrize("temperature_key", ["temp", "bt"])
def test_all_events(temperature_key):
    phases = ("turning_point", "dry_end", "fc_start", "fc_end", "sc_start", "drop")
    curve = {"events": [
        {"type": phase, "time": 40 + i * 100, temperature_key: 90 + i * 20}
        for i, phase in enumerate(phases)
    ]}
    fields = derive_roast_event_fields(curve)
    for i, phase in enumerate(phases):
        assert fields[f"{phase}_time"] == 40 + i * 100
        assert fields[f"{phase}_temp"] == 90 + i * 20


def test_tp_window_and_explicit_event_precedence():
    curve = {"time_seconds": [-1, 0, 39.8, 90, 91], "bt": [1, 200, 85, 90, 20]}
    assert derive_roast_event_fields(curve, infer_turning_point=True) == {
        "turning_point_time": 39, "turning_point_temp": 85,
    }
    curve["events"] = [{"type": "turning_point", "time": 42, "temp": 87}]
    assert derive_roast_event_fields(curve, infer_turning_point=True) == {
        "turning_point_time": 42, "turning_point_temp": 87,
    }


def test_missing_only_and_idempotency():
    roast = {"fc_start_time": 0, "drop_temp": 203, "curve_data": {"events": [
        {"type": "fc_start", "time": 480, "temp": 182},
        {"type": "drop", "time": 600, "temp": 199},
    ]}}
    changes = missing_roast_event_fields(roast)
    assert changes == {"fc_start_temp": 182, "drop_time": 600}
    roast.update(changes)
    assert missing_roast_event_fields(roast) == {}


def test_empty_partial_and_invalid_data():
    assert missing_roast_event_fields({"curve_data": None}) == {}
    curve = {"events": [None, {"type": "charge", "time": 0, "temp": 200},
                        {"type": "drop", "time": -1, "temp": float("nan")}],
             "time_seconds": [10, 20], "bt": [None]}
    assert derive_roast_event_fields(curve, infer_turning_point=True) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_backfill_modes(monkeypatch, capsys, apply):
    import runpy
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    namespace = runpy.run_path(str(Path(__file__).parents[2] / "scripts/backfill_roast_events.py"))
    backfill = namespace["backfill"]
    roast = {"_id": "completed-1", "curve_data": {"events": [
        {"type": "fc_start", "time": 480, "temp": 182},
    ]}, "fc_start_temp": 183}

    async def rows():
        yield roast

    collection = MagicMock()
    collection.find.return_value = rows()
    collection.update_one = AsyncMock()
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = collection
    config = MagicMock(mongodb_host="localhost", mongodb_port=27017, mongodb_name="maillard")
    config.load_mongodb_credentials.return_value = (None, None)
    monkeypatch.setitem(backfill.__globals__, "get_config", lambda: config)
    monkeypatch.setitem(backfill.__globals__, "AsyncIOMotorClient", lambda **_kwargs: client)
    await backfill(apply=apply)
    collection.find.assert_called_once_with({"status": "completed"})
    if apply:
        collection.update_one.assert_awaited_once_with(
            {"_id": "completed-1", "status": "completed", "fc_start_time": None},
            {"$set": {"fc_start_time": 480}},
        )
    else:
        collection.update_one.assert_not_awaited()
    client.close.assert_called_once()
    assert "completed-1\tfc_start_time\t480" in capsys.readouterr().out


def test_tp_not_inferred_without_real_dip():
    # Cold "charge" test row (22.8 C) and minimum at first sample: no TP.
    cold = {"time_seconds": [0.0, 1.6, 30.0], "bt": [22.8, 23.0, 40.0]}
    assert derive_roast_event_fields(cold, infer_turning_point=True) == {}
    flat = {"time_seconds": [0.0, 10.0, 40.0], "bt": [200.0, 195.0, 190.0]}
    assert derive_roast_event_fields(flat, infer_turning_point=True) == {}
