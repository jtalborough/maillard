"""Replay of a rising-RoR development and history exclusions."""

from maillard.ai.history import context_roast_issues
from maillard.roasting.development import DevelopmentController, DevSample


def _sample(
    *,
    bt: float,
    ror: float,
    heat: int,
    fc_marked: bool,
    elapsed: float | None = None,
    fc_elapsed: float | None = None,
    ai_heat: int = 75,
) -> DevSample:
    return DevSample(
        bt=bt,
        ror=ror,
        elapsed=elapsed,
        heat=heat,
        fan=40,
        drum=55,
        ai_heat=ai_heat,
        ai_fan=40,
        ai_drum=55,
        fc_predicted=186.0,
        fc_marked=fc_marked,
        fc_elapsed=fc_elapsed,
        drop_target=201.0,
        dev_min_s=90,
        dev_max_s=150,
        dtr_low=18.0,
        dtr_high=20.0,
    )


def test_replay_steps_heat_down_and_guards_second_crack():
    ctrl = DevelopmentController()
    heat = 75
    heats = []

    def step(bt, ror, fc_marked, elapsed=None, fc_elapsed=None, auto_drop=False):
        nonlocal heat
        cmd = ctrl.step(
            _sample(
                bt=bt,
                ror=ror,
                heat=heat,
                fc_marked=fc_marked,
                elapsed=elapsed,
                fc_elapsed=fc_elapsed,
            ),
            auto_drop=auto_drop,
        )
        heat = cmd.heat
        heats.append(heat)
        return cmd

    early = step(170, 8.0, False)
    assert early.heat == 75
    assert not early.constrained

    approach = step(182, 9.0, False)
    assert approach.heat == 70
    assert approach.reason == "approaching first crack"

    at_fc = step(186, 9.5, True, elapsed=603, fc_elapsed=603)
    assert at_fc.heat == 70
    assert at_fc.heat < 75
    assert at_fc.reason == "after first crack"

    for bt, ror, elapsed in ((190, 10.0, 630), (194, 11.0, 660), (198, 12.0, 690)):
        cmd = step(bt, ror, True, elapsed=elapsed, fc_elapsed=603)
        assert cmd.heat <= heats[-2]
        assert not cmd.drop_prompt

    prompt = step(201, 12.0, True, elapsed=720, fc_elapsed=603)
    assert prompt.drop_prompt
    assert not prompt.forced_prompt
    assert prompt.heat < at_fc.heat
    assert not prompt.auto_drop

    again = step(202, 12.2, True, elapsed=730, fc_elapsed=603)
    assert not again.drop_prompt

    guard = step(205, 12.4, True, elapsed=750, fc_elapsed=603)
    assert guard.forced_prompt
    assert guard.drop_prompt
    assert guard.heat > 0

    zero = step(207, 12.6, True, elapsed=760, fc_elapsed=603)
    assert zero.heat == 0
    assert zero.zero_heat
    assert heats == sorted(heats, reverse=True)


def test_development_floor_holds_band_and_blocks_a_cut_to_zero():
    ctrl = DevelopmentController()
    held = ctrl.step(
        _sample(bt=192, ror=9.0, heat=60, fc_marked=True, ai_heat=0, elapsed=640, fc_elapsed=600)
    )
    assert held.heat == 60
    low = ctrl.step(
        _sample(bt=193, ror=7.4, heat=55, fc_marked=True, ai_heat=0, elapsed=660, fc_elapsed=600)
    )
    assert low.heat == 55
    at_edge = ctrl.step(
        _sample(bt=194, ror=11.0, heat=50, fc_marked=True, ai_heat=0, elapsed=680, fc_elapsed=600)
    )
    assert at_edge.heat == 50

    cutting = DevelopmentController()
    heat = 75
    for _ in range(12):
        cmd = cutting.step(
            _sample(
                bt=194,
                ror=14.0,
                heat=heat,
                fc_marked=True,
                ai_heat=0,
                elapsed=700,
                fc_elapsed=600,
            )
        )
        heat = cmd.heat
        assert heat >= 35
    assert heat == 35

    custom = DevelopmentController(heat_floor=50)
    raised = custom.step(
        _sample(bt=190, ror=6.0, heat=0, fc_marked=True, ai_heat=0, elapsed=640, fc_elapsed=600)
    )
    assert raised.heat == 50
    one = custom.step(
        _sample(bt=192, ror=13.0, heat=70, fc_marked=True, ai_heat=0, elapsed=660, fc_elapsed=600)
    )
    assert one.heat == 65


def test_second_crack_guard_overrides_heat_floor():
    ctrl = DevelopmentController(heat_floor=35)
    guarded = ctrl.step(
        _sample(bt=207, ror=12.0, heat=40, fc_marked=True, ai_heat=80, elapsed=760, fc_elapsed=600)
    )
    assert guarded.heat == 0
    assert guarded.zero_heat
    assert guarded.reason == "second-crack guard"


def test_auto_drop_only_on_the_target_edge():
    ctrl = DevelopmentController()
    hit = ctrl.step(
        _sample(bt=201, ror=4.0, heat=40, fc_marked=True, elapsed=720, fc_elapsed=600),
        auto_drop=True,
    )
    assert hit.auto_drop
    assert hit.drop_prompt
    later = ctrl.step(
        _sample(bt=207, ror=4.0, heat=hit.heat, fc_marked=True, elapsed=760, fc_elapsed=600),
        auto_drop=True,
    )
    assert later.heat == 0
    assert not later.auto_drop


def _completed(**overrides):
    row = {
        "status": "completed",
        "total_time_seconds": 700,
        "drop_time": 700,
        "batch_size_grams": 450,
        "charge_temp": 200,
        "fc_start_time": 500,
        "drop_temp": 201,
        "roast_level": "Light",
    }
    row.update(overrides)
    return row


def test_context_excludes_junk_development():
    assert context_roast_issues(_completed()) == []
    early = context_roast_issues(_completed(fc_start_time=None, drop_temp=170))
    assert "dropped before first crack" in early
    low = context_roast_issues(_completed(fc_start_time=400, drop_temp=170))
    assert "drop far below level temperature" in low
    kept = _completed(drop_temp=None, fc_start_time=None, drop_time=None)
    assert "dropped before first crack" not in context_roast_issues(kept)
    assert "drop far below level temperature" not in context_roast_issues(kept)
