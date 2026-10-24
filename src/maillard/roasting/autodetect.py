"""Robust automatic detection of turning point (TP) and drop (discharge) from BT.

Both detectors work on a short moving-median-smoothed BT series, need a minimum
time window, and require the signal to stay confirmed for several samples so a
single noisy or stale reading can't trigger them. The operator can always mark
or clear events by hand; a manual mark wins and stops the detector.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median


@dataclass
class DetectedEvent:
    kind: str
    time_seconds: float
    temp: float


@dataclass
class EventAutoDetector:
    # Smoothing
    smooth_window: int = 3
    # Turning point: BT bottom, then a sustained rise
    tp_min_time: float = 20.0
    tp_max_time: float = 180.0
    tp_min_rise: float = 1.0          # C above the minimum (smoothed)
    tp_confirm_samples: int = 4       # consecutive rising smoothed samples
    tp_min_dip: float = 20.0          # BT must first fall this far below its start (beans charged)
    # Drop: sharp, sustained BT fall after discharge
    drop_min_time: float = 300.0      # never before 5 min into the roast
    drop_window: float = 20.0         # look-back window, seconds
    drop_min_fall: float = 12.0       # C fall from the recent peak within the window
    drop_min_rate: float = 25.0       # C/min average fall rate across the window
    drop_confirm_samples: int = 3     # consecutive falling smoothed samples

    _raw: deque = field(default_factory=lambda: deque(maxlen=7))
    _hist: deque = field(default_factory=lambda: deque(maxlen=200))
    _tp_min: tuple[float, float] | None = None
    _start_bt: float | None = None
    _tp_rising: int = 0
    _drop_falling: int = 0
    tp_done: bool = False
    drop_done: bool = False

    def reset_tp(self) -> None:
        self._tp_min, self._tp_rising = None, 0

    def update(self, t: float, bt: float) -> list[DetectedEvent]:
        """Feed one fresh sample; returns events detected on this sample."""
        self._raw.append(bt)
        s = median(list(self._raw)[-self.smooth_window:])
        prev = self._hist[-1][1] if self._hist else None
        self._hist.append((t, s))
        out: list[DetectedEvent] = []
        if not self.tp_done:
            ev = self._check_tp(t, s, prev)
            if ev:
                out.append(ev)
        if not self.drop_done:
            ev = self._check_drop(t, s, prev)
            if ev:
                out.append(ev)
        return out

    def _check_tp(self, t: float, s: float, prev: float | None) -> DetectedEvent | None:
        if self._start_bt is None:
            self._start_bt = s
        if t > self.tp_max_time:
            self.tp_done = True
            return None
        if self._tp_min is None or s < self._tp_min[1]:
            self._tp_min, self._tp_rising = (t, s), 0
            return None
        if prev is not None and s > prev:
            self._tp_rising += 1
        elif prev is not None and s < prev:
            self._tp_rising = 0
        mt, mv = self._tp_min
        if (t >= self.tp_min_time and self._start_bt - mv >= self.tp_min_dip
                and self._tp_rising >= self.tp_confirm_samples
                and s - mv >= self.tp_min_rise):
            self.tp_done = True
            return DetectedEvent("turning_point", round(mt, 1), round(mv, 1))
        return None

    def _check_drop(self, t: float, s: float, prev: float | None) -> DetectedEvent | None:
        if prev is not None and s < prev:
            self._drop_falling += 1
        else:
            self._drop_falling = 0
        if t < self.drop_min_time or self._drop_falling < self.drop_confirm_samples:
            return None
        window = [(ht, hv) for ht, hv in self._hist if t - ht <= self.drop_window]
        if len(window) < 3:
            return None
        pt, pv = max(window, key=lambda x: x[1])
        fall, span = pv - s, max(t - pt, 1e-6)
        if fall >= self.drop_min_fall and fall / span * 60 >= self.drop_min_rate:
            self.drop_done = True
            return DetectedEvent("drop", round(pt, 1), round(pv, 1))
        return None


__all__ = ["DetectedEvent", "EventAutoDetector"]
