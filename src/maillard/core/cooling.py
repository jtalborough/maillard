"""Cooling-tray fan (Kaleido CS) control with confirmation and auto-off."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_logger = logging.getLogger(__name__)


class CoolingFanController:
    """Tracks cooling-tray fan state, CS confirmation, and timed auto-off."""

    def __init__(self, duration_seconds: float = 240.0) -> None:
        self._duration_seconds = max(0.01, float(duration_seconds))
        self._enabled = False
        self._confirmed: bool | None = None
        self._auto = False
        self._off_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._driver: Any | None = None

    def configure(self, duration_seconds: float) -> None:
        self._duration_seconds = max(0.01, float(duration_seconds))

    def get_status(self) -> dict[str, Any]:
        remaining = None
        if self._enabled and self._off_at is not None:
            remaining = max(0.0, round(self._off_at - time.monotonic(), 1))
        return {
            "enabled": self._enabled,
            "confirmed": self._confirmed,
            "auto": self._auto,
            "duration_seconds": self._duration_seconds,
            "remaining_seconds": remaining,
        }

    async def set_enabled(
        self,
        driver: Any,
        enabled: bool,
        *,
        auto: bool = False,
        schedule_off: bool | None = None,
    ) -> dict[str, Any]:
        self._cancel_timer()
        self._driver = driver
        want = 1 if enabled else 0
        confirmed = await self._send_cs(driver, want)
        self._enabled = enabled
        self._confirmed = confirmed
        self._auto = bool(auto and enabled)
        if schedule_off is None:
            schedule_off = self._auto
        if enabled and schedule_off:
            self._off_at = time.monotonic() + self._duration_seconds
            self._task = asyncio.create_task(
                self._auto_off_after(self._duration_seconds),
                name="cooling-fan-auto-off",
            )
        else:
            self._off_at = None
            if not enabled:
                self._auto = False
        level = _logger.info if confirmed else _logger.warning
        level(
            "Cooling fan %s (CS=%s, confirmed=%s, auto=%s)",
            "ON" if enabled else "OFF",
            want,
            confirmed,
            self._auto,
        )
        return self.get_status()

    async def stop(self) -> None:
        self._cancel_timer()
        self._enabled = False
        self._confirmed = None
        self._auto = False
        self._off_at = None
        self._driver = None

    def _cancel_timer(self) -> None:
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _auto_off_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            driver = self._driver
            if driver is None or not self._enabled:
                return
            self._task = None
            await self.set_enabled(driver, False, auto=False, schedule_off=False)
        except asyncio.CancelledError:
            return

    @staticmethod
    async def _send_cs(driver: Any, want: int) -> bool:
        if driver is None or not getattr(driver, "is_connected", False):
            _logger.warning("Cooling fan CS=%s not sent: roaster not connected", want)
            return False
        try:
            send = getattr(driver, "_send_message", None)
            if callable(send):
                await send("CS", str(want))
            elif hasattr(driver, "_sim_state"):
                driver._sim_state.cs = want
            else:
                _logger.warning("Cooling fan CS=%s unsupported on this driver", want)
                return False
        except Exception:
            _logger.warning("Cooling fan CS=%s send failed", want, exc_info=True)
            return False

        confirmed = False
        for _ in range(15):
            if CoolingFanController._read_cs(driver) == want:
                confirmed = True
                break
            await asyncio.sleep(0.1)
        if not confirmed:
            _logger.warning(
                "Cooling fan CS=%s sent but roaster did not confirm within 1.5s "
                "(physical CS never verified on this M10 — check tray fans)",
                want,
            )
        return confirmed

    @staticmethod
    def _read_cs(driver: Any) -> int | None:
        getter = getattr(driver, "get_state_value", None)
        if callable(getter):
            value = getter("CS")
            if isinstance(value, (int, float)):
                return int(value)
        state = getattr(driver, "state", None)
        if isinstance(state, dict):
            value = state.get("CS")
            if isinstance(value, (int, float)):
                return int(value)
        sim = getattr(driver, "_sim_state", None)
        if sim is not None and hasattr(sim, "cs"):
            return int(sim.cs)
        return None
