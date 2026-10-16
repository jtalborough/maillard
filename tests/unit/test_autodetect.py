from maillard.roasting.autodetect import EventAutoDetector


def _run(points):
    d = EventAutoDetector()
    out = []
    for t, bt in points:
        out += d.update(t, bt)
    return out


def _roast(drop_at=None, total=800):
    pts, bt = [], 200.0
    t = 0.0
    while t <= total:
        if t < 45:
            bt = 200 - (200 - 75) * (t / 45)
        elif drop_at is None or t < drop_at:
            bt = 75 + (t - 45) * 0.17
        else:
            bt -= 2.5  # ~100 C/min fall after discharge
        pts.append((round(t, 1), round(bt, 1)))
        t += 1.5
    return pts


def test_tp_detected_near_bottom_once():
    evs = [e for e in _run(_roast()) if e.kind == "turning_point"]
    assert len(evs) == 1 and 40 <= evs[0].time_seconds <= 50


def test_no_drop_without_fall_and_single_glitch_ignored():
    pts = _roast()
    pts[300] = (pts[300][0], pts[300][1] - 40)  # one bad sample mid-roast
    assert not [e for e in _run(pts) if e.kind == "drop"]


def test_drop_detected_at_discharge():
    evs = [e for e in _run(_roast(drop_at=720, total=760)) if e.kind == "drop"]
    assert len(evs) == 1 and 715 <= evs[0].time_seconds <= 722


def test_no_tp_when_beans_never_charged():
    assert not _run([(i * 1.5, 200.0 + (i % 2) * 0.2) for i in range(100)])
