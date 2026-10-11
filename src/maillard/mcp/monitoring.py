"""Background roast monitoring: polls driver state and publishes to EventBus.

The RoastMonitor bridges the hardware driver and the event system during
active roasts. It periodically calls ``driver.read_state()``, wraps the
result in a :class:`StateChangeEvent`, and publishes it to the
:class:`EventBus`. The RoastSession's existing handler then records
each sample into the CurveCollector.

The monitor also emits structured INFO log lines per sample so operators
can ``tail -f`` the log file for real-time visibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING

from maillard.core.events import SafetyAlertEvent, SafetyAlertLevel, StateChangeEvent

if TYPE_CHECKING:
    from maillard.core.events import EventBus
    from maillard.driver.base import RoasterDriver
    from maillard.roasting.session import RoastSession

_logger = logging.getLogger(__name__)


class RoastMonitor:
    """Background polling loop: driver -> EventBus during active roasts."""

    def __init__(
        self,
        driver: RoasterDriver,
        event_bus: EventBus,
        session: RoastSession,
        *,
        poll_interval: float = 1.5,
        max_consecutive_errors: int = 5,
        stale_alarm_seconds: float = 15.0,
        alarm_clear_seconds: float = 5.0,
    ) -> None:
        self._stale_alarm_seconds = stale_alarm_seconds
        self._alarm_clear_seconds = alarm_clear_seconds
        self._previous_sample_time: float | None = getattr(driver, "last_bt_sample_time", None)
        self._fresh_since: float | None = None
        self._link_state = "ok"
        self._driver = driver
        self._event_bus = event_bus
        self._session = session
        self._poll_interval = poll_interval
        self._max_consecutive_errors = max_consecutive_errors
        self._task: asyncio.Task[None] | None = None
        self._consecutive_errors = 0
        sample_time = getattr(self._driver, "last_bt_sample_time", None)
        self._last_valid_bt = sample_time if isinstance(sample_time, (int, float)) else time.monotonic()
        self._connection_alarm = False
        self._alarm_started: float | None = None
        self._freshness_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Create and start the background asyncio.Task."""
        if self._task is not None and not self._task.done():
            return
        sample_time = getattr(self._driver, "last_bt_sample_time", None)
        self._last_valid_bt = sample_time if isinstance(sample_time, (int, float)) else time.monotonic()
        if hasattr(self._driver, "add_connection_listener"):
            self._driver.add_connection_listener(self._on_connection_event)
        self._freshness_task = asyncio.create_task(self._freshness_loop(), name="sample-freshness")
        self._task = asyncio.create_task(self._loop(), name="roast-monitor")
        _logger.info("Roast monitor started (interval=%.1fs)", self._poll_interval)

    async def stop(self) -> None:
        """Cancel task, await completion. Idempotent."""
        if hasattr(self._driver, "remove_connection_listener"):
            self._driver.remove_connection_listener(self._on_connection_event)
        if self._task is None:
            return
        if self._freshness_task is not None:
            self._freshness_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._freshness_task
            self._freshness_task = None
        task = self._task
        self._task = None
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._connection_alarm:
            self._connection_alarm = False
            self._alarm_started = None
            await self._event_bus.publish(SafetyAlertEvent(
                level=SafetyAlertLevel.CRITICAL, message="Roast monitoring stopped",
                condition="connection_lost", cleared=True,
            ))
        _logger.info("Roast monitor stopped")

    @property
    def connection_lost(self) -> bool:
        """Whether a connection/stale-data alarm is active."""
        return self._connection_alarm

    @property
    def is_running(self) -> bool:
        """Whether the polling task is active."""
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        """Poll driver.read_state(), publish StateChangeEvent, log sample."""
        try:
            while True:
                await self._poll_once()
                delay = min(5.0, self._poll_interval * (1 + self._consecutive_errors))
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

    async def _freshness_loop(self) -> None:
        """Detect loss even while a read is blocked waiting for hardware."""
        try:
            while True:
                await self._check_freshness()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    def _on_connection_event(self, event: dict) -> None:
        if self._session.is_active:
            self._session.record_connection_event(event)

    @property
    def link_state(self) -> str:
        return "lost" if self._connection_alarm else self._link_state

    @property
    def data_age_s(self) -> float:
        return max(0.0, time.monotonic() - self._last_valid_bt)

    async def _check_freshness(self, *, fresh: bool = False) -> None:
        sample_time = getattr(self._driver, "last_bt_sample_time", None)
        if isinstance(sample_time, (int, float)):
            self._last_valid_bt = max(self._last_valid_bt, sample_time)
        disconnected = not self._driver.is_connected
        lost = (self.data_age_s >= self._stale_alarm_seconds
                or (disconnected and getattr(self._driver, "reconnect_failures", 0) >= 1)
                or self._consecutive_errors >= self._max_consecutive_errors)
        self._link_state = "lost" if lost else "degraded" if disconnected or self.data_age_s > 5 else "ok"
        if lost:
            self._fresh_since = None
            await self._set_connection_alarm(True)
        elif (fresh and self._connection_alarm and self._fresh_since is not None
              and time.monotonic() - self._fresh_since >= self._alarm_clear_seconds):
            await self._set_connection_alarm(False)

    async def _set_connection_alarm(self, lost: bool) -> None:
        if lost == self._connection_alarm:
            return
        if (not lost and self._alarm_started is not None
                and self._last_valid_bt <= self._alarm_started):
            return  # Reconnection alone does not prove that BT data resumed.
        self._connection_alarm = lost
        self._alarm_started = time.monotonic() if lost else None
        message = ("CONNECTION LOST / BT DATA STALE - check roaster, use Kaleido controls"
                   if lost else "Roaster BT data resumed")
        if lost:
            _logger.critical(message)
        else:
            _logger.info(message)
        await self._event_bus.publish(SafetyAlertEvent(
            level=SafetyAlertLevel.CRITICAL, message=message,
            condition="connection_lost", cleared=not lost,
        ))

    async def _poll_once(self) -> None:
        """Execute a single poll cycle."""
        try:
            state = await self._driver.read_state()
        except Exception:
            self._consecutive_errors += 1
            _logger.warning(
                "Monitor poll error (%d/%d consecutive)",
                self._consecutive_errors,
                self._max_consecutive_errors,
                exc_info=True,
            )
            if self._consecutive_errors >= self._max_consecutive_errors:
                await self._set_connection_alarm(True)
            self._fresh_since = None
            await self._check_freshness()
            await self._event_bus.publish(StateChangeEvent(
                state=getattr(self._driver, "state", {}), stale=True, data_age_s=self.data_age_s,
                connected=self._driver.is_connected,
            ))
            return

        # Reset error counter on success
        self._consecutive_errors = 0

        fresh = False
        bt = state.get("BT")
        if isinstance(bt, (int, float)) and math.isfinite(bt) and bt >= 0:
            # Kaleido timestamps actual received samples, not repeated cache reads.
            sample_time = getattr(self._driver, "last_bt_sample_time", None)
            if isinstance(sample_time, (int, float)):
                fresh = self._previous_sample_time is None or sample_time > self._previous_sample_time
                if fresh:
                    self._previous_sample_time = sample_time
                self._last_valid_bt = sample_time
            elif not hasattr(self._driver, "last_bt_sample_time"):
                fresh = True
                self._last_valid_bt = time.monotonic()
        fresh = fresh and self._driver.is_connected
        if fresh:
            if self._fresh_since is None:
                self._fresh_since = time.monotonic()
        else:
            self._fresh_since = None
        await self._check_freshness(fresh=fresh)

        # Publish event
        event = StateChangeEvent(state=state, stale=not fresh, data_age_s=self.data_age_s,
                                 connected=self._driver.is_connected)
        await self._event_bus.publish(event)

        # Structured log line for operator visibility
        bt = state.get("BT")
        et = state.get("ET")
        elapsed = self._session.elapsed_time
        phase = self._session.current_phase
        ror = self._session.latest_ror

        _logger.info(
            "SAMPLE | elapsed=%.1fs | BT=%.1f | ET=%.1f | RoR=%s | phase=%s",
            elapsed if elapsed is not None else 0.0,
            bt if bt is not None else 0.0,
            et if et is not None else 0.0,
            f"{ror:.1f}" if ror is not None else "N/A",
            phase.name if phase is not None else "unknown",
        )


__all__ = ["RoastMonitor"]
