"""Cooling-tray fan (Kaleido CS) control with confirmation and auto-off."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from maillard.core.events import SafetyAlertEvent, SafetyAlertLevel

_logger = logging.getLogger(__name__)

UNCONFIRMED_MESSAGE = "cooling unconfirmed, switch tray fans on"


class CoolingFanController:
    """Tracks cooling-tray fan state, CS confirmation, and timed auto-off."""

    def __init__(
        self,
        duration_seconds: float = 240.0,
        *,
        event_bus: Any | None = None,
    ) -> None:
        self._duration_seconds = max(0.01, float(duration_seconds))
        self._event_bus = event_bus
        self._enabled = False
        self._confirmed: bool | None = None
        self._auto = False
        self._off_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._driver: Any | None = None
        self._needs_resend = False

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
        self._needs_resend = False
        if enabled and not confirmed:
            await self._publish_unconfirmed(True)
        elif confirmed:
            await self._publish_unconfirmed(False)
        if enabled:
            self._ensure_watch()
        else:
            await self._cancel_watch()
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
        await self._cancel_watch()
        self._enabled = False
        self._confirmed = None
        self._auto = False
        self._off_at = None
        self._needs_resend = False
        self._driver = None

    def _cancel_timer(self) -> None:
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _cancel_watch(self) -> None:
        task = self._watch_task
        self._watch_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _ensure_watch(self) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            return
        self._watch_task = asyncio.create_task(self._watch_link(), name="cooling-fan-link")

    async def _watch_link(self) -> None:
        try:
            while self._enabled:
                await asyncio.sleep(0.3)
                driver = self._driver
                if driver is None or not self._enabled:
                    continue
                if not getattr(driver, "is_connected", False):
                    self._needs_resend = True
                    continue
                if not self._needs_resend:
                    continue
                self._needs_resend = False
                confirmed = await self._send_cs(driver, 1)
                self._confirmed = confirmed
                await self._publish_unconfirmed(not confirmed)
        except asyncio.CancelledError:
            return

    async def _publish_unconfirmed(self, active: bool) -> None:
        bus = self._event_bus
        if bus is None:
            return
        await bus.publish(
            SafetyAlertEvent(
                level=SafetyAlertLevel.CRITICAL,
                condition="cooling_unconfirmed",
                message=UNCONFIRMED_MESSAGE,
                cleared=not active,
            )
        )

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

        if await CoolingFanController._confirm_cs(driver, want):
            return True
        _logger.warning("Cooling fan CS=%s not confirmed; retrying once", want)
        try:
            await CoolingFanController._transmit_cs(driver, want)
        except Exception:
            _logger.warning("Cooling fan CS=%s retry failed", want, exc_info=True)
            return False
        confirmed = await CoolingFanController._confirm_cs(driver, want)
        if not confirmed:
            _logger.warning(
                "Cooling fan CS=%s sent twice but roaster did not confirm",
                want,
            )
        return confirmed

    @staticmethod
    async def _transmit_cs(driver: Any, want: int) -> None:
        send = getattr(driver, "_send_message", None)
        if callable(send):
            await send("CS", str(want))
            return
        if hasattr(driver, "_sim_state"):
            driver._sim_state.cs = want
            return
        raise RuntimeError("Cooling fan CS unsupported on this driver")

    @staticmethod
    async def _confirm_cs(driver: Any, want: int) -> bool:
        for _ in range(8):
            if CoolingFanController._read_cs(driver) == want:
                return True
            await asyncio.sleep(0.1)
        return False

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
