"""Probe calibration and shared planner/live expectations."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.ai import calibration
from maillard.ai.agent import SYSTEM_PROMPT, RoastAIAgent
from maillard.ai.generate import compute_heuristic_profile
from maillard.core.events import EventBus
from maillard.core.safety import SafetyLimits, SafetyMonitor
from maillard.roasting.plan import validate_plan
from maillard.roasting.state import RoastPhase


@pytest.fixture
def roasts():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/roast_history_fixture.json").read_text()
    )


@pytest.fixture(autouse=True)
def default_cache(monkeypatch):
    monkeypatch.setattr(calibration, "_cached", calibration.RoasterCalibration())
    monkeypatch.setattr(calibration, "_loaded_at", 0)


def test_fixture_fit_and_exclusions(roasts):
    c = calibration.fit_calibration(roasts)
    assert c.accepted_roasts == 16
    assert c.excluded_roasts == 8
    assert 180 <= c.fc_start_temp <= 185
    assert 360 <= c.fc_time.at(450) <= 480
    assert 540 <= c.fc_time.at(750) <= 720
    assert 25 <= c.tp_time_s <= 50
    assert 78 <= c.tp_temp <= 105
    assert c.max_observed_et == 199.8
    assert c.sources["drop_temps.dark"] == "default (low confidence)"
    assert c.sources["fc_start_temp"] == "fitted (n=16)"


def test_persisted_phases_take_precedence_and_late_drop_retains_fc(roasts):
    valid = [
        r
        for r in roasts
        if r["status"] == "completed"
        and r.get("drop_time")
        and 180 <= r["drop_time"] <= 1500
        and r["charge_temp"] >= 150
        and r["batch_size_grams"] >= 250
    ]
    docs = []
    for r in valid:
        d = dict(r)
        d["curve_data"] = {"events": d.pop("events")}
        d["fc_start_temp"] = 184
        d["drop_temp"] = 160
        docs.append(SimpleNamespace(model_dump=lambda d=d: d))
    c = calibration.fit_calibration(docs)
    assert c.accepted_roasts == 15
    assert c.fc_start_temp == 184
    assert c.sources["drop_temps.light"] == "default (low confidence)"
    assert c.sources["fc_time"] == "fitted (n=15, Theil-Sen)"


def test_sparse_fc_falls_back():
    c = calibration.fit_calibration(
        [
            {
                "status": "completed",
                "batch_size_grams": 500,
                "charge_temp": 200,
                "drop_time": 550,
                "fc_start_time": 430,
                "fc_start_temp": 182,
            }
        ]
        * 3
    )
    assert c.sources["fc_time"] == "default (low confidence)"
    assert c.fc_time.at(450) == calibration.RoasterCalibration().fc_time.at(450)


def test_batch_warning_shared_by_planner_and_plan():
    c = calibration.get_calibration(1200)
    assert c["extrapolated"]
    assert f"{c['fc_time']['max_g']:g} g max observed" in c["warning"]
    assert calibration.get_calibration(750)["extrapolated"] is False
    p = compute_heuristic_profile(None, batch_size_grams=1200)
    assert c["warning"] in p.ai_rationale
    assert c["warning"] in p.phases[0]["notes"]
    assert c["warning"] in validate_plan(
        p.phases, charge_temp=p.charge_temp, batch_size_grams=1200, target_dtr=p.target_dtr
    )
    p = compute_heuristic_profile(None, batch_size_grams=450)
    assert p.phases[4]["target_temp"] == calibration.get_calibration(450)["fc_start_temp"]
    assert p.phases[4]["target_time_seconds"] == round(calibration.get_calibration(450)["fc_time_s"])


@pytest.mark.asyncio
async def test_loader_caches_db_and_falls_back(monkeypatch, roasts):
    from maillard.db.models import Roast

    query = SimpleNamespace(to_list=AsyncMock(return_value=roasts))
    find = MagicMock(return_value=query)
    monkeypatch.setattr(Roast, "find", find)
    fitted = await calibration.load_calibration()
    assert fitted.accepted_roasts == 16
    assert await calibration.load_calibration() is fitted
    find.assert_called_once()
    find.side_effect = RuntimeError("unavailable")
    defaults = await calibration.load_calibration(refresh=True)
    assert defaults.accepted_roasts == 0
    assert defaults.fc_start_temp == 182


def test_live_prompt_includes_locked_targets_and_calibrated_fc():
    session = SimpleNamespace(
        elapsed_time=400,
        latest_ror=7,
        current_phase=None,
        machine=SimpleNamespace(dry_end_time=280, fc_start_time=None),
        collector=SimpleNamespace(samples=[], events=[]),
    )
    state = SimpleNamespace(
        roast_session=session,
        driver=SimpleNamespace(state={"BT": 180, "ET": 175, "HP": 40, "FC": 50, "RC": 60}),
        event_bus=EventBus(),
    )
    agent = RoastAIAgent(state, SimpleNamespace(), model="test")
    agent._batch_grams = 450
    agent._locked_plan = {
        "phases": compute_heuristic_profile(None, batch_size_grams=450).phases,
        "warnings": ["Test plan warning"],
    }
    messages = agent._build_messages()
    content = messages[0]["content"]
    assert "Locked RoastPlan targets" in content
    c = calibration.get_calibration(450)
    assert f"fc_start: {round(c['fc_time_s'])}s BT={c['fc_start_temp']:g}" in content
    assert "heat=40 fan=50 drum=60" in content
    assert "Plan delta" in content
    assert "Test plan warning" in content
    assert "182.0C" in agent.get_prompt_snapshot()["system_prompt"]
    assert "190-205" not in SYSTEM_PROMPT
    assert "80-100%" not in SYSTEM_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,bt,expected",
    [
        (RoastPhase.MAILLARD, 165, True),
        (RoastPhase.MAILLARD, 178, False),
        (RoastPhase.MAILLARD, 145, False),
        (RoastPhase.DEVELOPMENT, 190, False),
        (RoastPhase.DRYING, 140, False),
    ],
)
async def test_stall_warning_only_in_pre_fc_window(phase, bt, expected):
    import time

    session = SimpleNamespace(current_phase=phase)
    driver = SimpleNamespace(emergency_stop=AsyncMock())
    from maillard.core.config import MaillardConfig

    monitor = SafetyMonitor(driver, EventBus(), session, MaillardConfig())
    monitor._low_ror_start_time = time.time() - 31
    await monitor._check_ror_limits(2, bt)
    assert ("ror_stall" in monitor._alerted_conditions) is expected
    driver.emergency_stop.assert_not_called()
    if not expected:
        assert monitor._low_ror_start_time is None


def test_temperature_defaults():
    from maillard.core.config import MaillardConfig

    assert SafetyLimits().max_et_temp == MaillardConfig().max_et_temp == 240
    assert SafetyLimits().max_bt_temp == 230
    assert MaillardConfig().ai_auto_control_enabled is False


@pytest.mark.asyncio
async def test_locked_plan_is_loaded_from_roast_document(monkeypatch):
    from maillard.db.models import Roast, RoastPlan

    plan = SimpleNamespace(
        locked_at="2026-10-02",
        model_dump=lambda: {
            "phases": compute_heuristic_profile(None, batch_size_grams=450).phases,
            "batch_size_grams": 450,
            "warnings": [],
        },
    )
    monkeypatch.setattr(calibration, "_loaded_at", __import__("time").monotonic())
    monkeypatch.setattr(Roast, "get", AsyncMock(return_value=SimpleNamespace(plan_id="p")))
    get_plan = AsyncMock(return_value=plan)
    monkeypatch.setattr(RoastPlan, "get", get_plan)
    state = SimpleNamespace(
        roast_session=SimpleNamespace(roast_id="r", coffee_id=None), event_bus=EventBus()
    )
    agent = RoastAIAgent(state, SimpleNamespace(), model="test")
    await agent._resolve_roast_history()
    await agent._resolve_roast_history()
    assert agent._locked_plan["phases"][4]["target_temp"] == 182
    get_plan.assert_awaited_once_with("p")


@pytest.mark.asyncio
async def test_simulator_et_ceiling_stops_heat():
    """Exercise the new ceiling through the simulated driver, without hardware."""
    from maillard.core.config import MaillardConfig
    from maillard.driver.simulator import SimulatorDriver

    driver = SimulatorDriver()
    await driver.connect("localhost", 80)
    try:
        await driver.set_heating_state(True)
        await driver.set_heat(50)
        monitor = SafetyMonitor(driver, EventBus(), SimpleNamespace(), MaillardConfig())
        await monitor._check_et_limit(200)
        assert not monitor.recent_alerts
        await monitor._check_et_limit(241)
        assert driver.state["HP"] == 0
        assert monitor.recent_alerts[-1].condition == "et_over_limit"
    finally:
        await driver.disconnect()


def _fit_roast(grams, **overrides):
    return {
        "status": "completed",
        "batch_size_grams": grams,
        "charge_temp": 199,
        "total_time_seconds": 280 + 0.6 * grams,
        "fc_start_time": 210 + 0.5 * grams,
        "fc_start_temp": 182,
        "turning_point_time": 40,
        "turning_point_temp": 88,
        **overrides,
    }


def test_theil_sen_ignores_single_outlier():
    rows = [_fit_roast(g, charge_temp=190 + 0.01 * g) for g in (300, 400, 500, 600, 700, 800)]
    baseline = calibration.fit_calibration(rows)
    outlier = _fit_roast(550, fc_start_time=1000, total_time_seconds=1400, charge_temp=240)
    fitted = calibration.fit_calibration([*rows, outlier])
    assert fitted.accepted_roasts == len(rows) + 1
    for field in ("fc_time", "total_time", "charge_temp"):
        expected, actual = getattr(baseline, field), getattr(fitted, field)
        assert actual.a == pytest.approx(expected.a)
        assert actual.b == pytest.approx(expected.b)
        assert actual.at(750) == pytest.approx(expected.at(750))


def test_drop_temperatures_pool_adjacent_violators():
    rows = [
        _fit_roast(450, roast_level="light", drop_temp=208),
        _fit_roast(500, roast_level="medium-light", drop_temp=206),
        _fit_roast(600, roast_level="medium", drop_temp=200),
        _fit_roast(700, roast_level="medium", drop_temp=200),
    ]
    fitted = calibration.fit_calibration(rows)
    drops = list(fitted.drop_temps.values())
    assert drops == sorted(drops)
    # Pool the three conflicting levels, weighted by their roast counts.
    pooled = (208 + 206 + 2 * 200) / 4
    for level in ("light", "medium-light", "medium"):
        assert fitted.drop_temps[level] == pytest.approx(round(pooled, 1))
        assert "pooled for monotonic levels" in fitted.sources[f"drop_temps.{level}"]


def test_flat_charge_fit_uses_extrapolation_physics_prior():
    fitted = calibration.fit_calibration([_fit_roast(g) for g in (300, 500, 800)])
    assert fitted.charge_temp.b == pytest.approx(0)
    assert fitted.charge_temp.at(500) == pytest.approx(199)
    assert fitted.charge_temp.at(1200) >= fitted.charge_temp.at(800) + 0.025 * 400


def test_cold_charge_roast_excluded_from_all_fits():
    rows = [_fit_roast(g) for g in (300, 500, 800)]
    baseline = calibration.fit_calibration(rows)
    low_charge = _fit_roast(600, charge_temp=22.8, fc_start_temp=184, turning_point_time=50)
    fitted = calibration.fit_calibration([*rows, low_charge])
    assert fitted.accepted_roasts == len(rows)
    assert fitted.excluded_roasts == 1
    assert fitted.sources["fc_time"] == "fitted (n=3, Theil-Sen)"
    assert fitted.sources["fc_start_temp"] == "fitted (n=3)"
    assert fitted.sources["tp_time_s"] == "fitted (n=3)"
    assert fitted.charge_temp == baseline.charge_temp
    assert fitted.sources["charge_temp"] == "fitted (n=3, Theil-Sen)"
