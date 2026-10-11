"""Heating manager for reliable Kaleido M10 heat control.

The Kaleido M10 firmware reverts the HS (heating state) variable to 0
unless continuously maintained. This module provides a HeatingManager
that sends HS=1 at regular intervals to keep heating active.

The HeatingManager runs as a background asyncio task and follows the
same lifecycle pattern as RoastMonitor and SafetyMonitor.

Usage:
    manager = HeatingManager(driver)
    await manager.start(heat_percent=80)
    # ... heating is now maintained ...
    await manager.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from maillard.core.events import EventBus, SafetyAlertEvent, SafetyAlertLevel

if TYPE_CHECKING:
    from maillard.driver.base import RoasterDriver

_logger = logging.getLogger(__name__)


@dataclass
class HeatingStatus:
    """Status snapshot of the HeatingManager."""

    active: bool
    heat_percent: int
    send_interval: float
    last_send_time: float | None
    consecutive_errors: int
    total_sends: int
    total_errors: int


class HeatingManager:
    """Maintains HS=1 for reliable Kaleido M10 heating.

    The Kaleido M10 firmware reverts HS to 0 unless continuously re-sent.
    This manager runs a background task that sends HS=1 at regular intervals
    to keep the heating element active.

    The manager also sends the current HP (heat power) value to ensure
    both HS and HP are synchronized.

    Usage:
        manager = HeatingManager(driver)
        await manager.start(heat_percent=80)
        # Heating is now maintained at 80%
        await manager.update_heat(60)  # Change to 60%
        await manager.stop()  # Stop heating

    Attributes:
        heat_percent: Current heat power percentage
        is_running: Whether the manager is actively maintaining heat
    """

    def __init__(
        self,
        driver: RoasterDriver,
        *,
        send_interval: float = 1.0,
        max_consecutive_errors: int = 5,
        event_bus: EventBus | None = None,
    ) -> None:
        """Initialize the HeatingManager.

        Args:
            driver: Kaleido driver instance (must have _send_message method)
            send_interval: Seconds between HS=1 sends (default 1.0)
            max_consecutive_errors: Stop after this many consecutive failures
        """
        self._driver = driver
        self._event_bus = event_bus
        self._paused_disconnected = False
        self._connection_generation = getattr(driver, "connection_generation", None)
        self._preheat_deadline: float | None = None
        self._send_interval = send_interval
        self._max_consecutive_errors = max_consecutive_errors

        # State
        self._heat_percent: int = 0
        self._task: asyncio.Task[None] | None = None
        self._consecutive_errors = 0
        self._total_sends = 0
        self._total_errors = 0
        self._last_send_time: float | None = None

    @property
    def heat_percent(self) -> int:
        """Current heat power percentage."""
        return self._heat_percent

    @property
    def is_running(self) -> bool:
        """Check if the manager is actively maintaining heat."""
        return self._task is not None and not self._task.done()

    async def start(
        self, heat_percent: int = 100, *, preheat_timeout: float | None = None
    ) -> None:
        """Start maintaining heating at the specified power level.

        Begins the background task that continuously sends HS=1 and HP
        commands to maintain heating.

        Args:
            heat_percent: Heat power percentage (0-100, default 100)

        Raises:
            ValueError: If heat_percent is not in range 0-100
        """
        if not 0 <= heat_percent <= 100:
            raise ValueError(f"heat_percent must be 0-100, got {heat_percent}")

        # Only an explicit start/heat command can release the disconnect latch.
        if self._paused_disconnected:
            await self._alert("heating_disconnected", "Operator resumed heating", cleared=True)
        self._paused_disconnected = False
        self._connection_generation = getattr(self._driver, "connection_generation", None)
        if preheat_timeout is not None:
            await self._alert("preheat_timeout", "Operator restarted preheat", cleared=True)
            self._preheat_deadline = time.monotonic() + preheat_timeout

        if self.is_running:
            # Already running - just update the heat percent
            self._heat_percent = heat_percent
            _logger.debug("HeatingManager already running, updated to %d%%", heat_percent)
            return

        self._heat_percent = heat_percent
        self._consecutive_errors = 0

        self._task = asyncio.create_task(self._loop(), name="heating-manager")
        _logger.info(
            "HeatingManager started (heat=%d%%, interval=%.1fs)",
            heat_percent,
            self._send_interval,
        )

    async def stop(self) -> None:
        """Stop maintaining heating.

        Cancels the background task and sends HS=0 to disable heating.
        Idempotent - safe to call multiple times.
        """
        if self._task is None:
            return

        task = self._task
        self._task = None

        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._preheat_deadline = None
        await self._disable_heat()
        if self._paused_disconnected:
            self._paused_disconnected = False
            await self._alert("heating_disconnected", "Local heating stopped", cleared=True)

        _logger.info(
            "HeatingManager stopped (total_sends=%d, total_errors=%d)",
            self._total_sends,
            self._total_errors,
        )

    async def update_heat(self, heat_percent: int) -> None:
        """Update the heat power percentage.

        Changes the HP value that will be sent on the next loop iteration.

        Args:
            heat_percent: New heat power percentage (0-100)

        Raises:
            ValueError: If heat_percent is not in range 0-100
        """
        if not 0 <= heat_percent <= 100:
            raise ValueError(f"heat_percent must be 0-100, got {heat_percent}")

        self._heat_percent = heat_percent
        _logger.debug("HeatingManager heat updated to %d%%", heat_percent)

    def get_status(self) -> dict[str, Any]:
        """Get current manager status.

        Returns:
            Dict with status information
        """
        return {
            "active": self.is_running and not self._paused_disconnected,
            "state": ("paused_disconnected" if self._paused_disconnected
                      else "heating" if self.is_running else "stopped"),
            "heat_percent": self._heat_percent,
            "send_interval": self._send_interval,
            "last_send_time": self._last_send_time,
            "consecutive_errors": self._consecutive_errors,
            "total_sends": self._total_sends,
            "total_errors": self._total_errors,
        }

    def finish_preheat(self) -> None:
        """Charge ends the preheat timeout without changing the heat setting."""
        self._preheat_deadline = None

    async def _alert(self, condition: str, message: str, *, cleared: bool = False) -> None:
        if not cleared:
            _logger.critical(message)
        if self._event_bus is not None:
            await self._event_bus.publish(SafetyAlertEvent(
                level=SafetyAlertLevel.CRITICAL, condition=condition, message=message, cleared=cleared,
            ))

    async def _disable_heat(self) -> None:
        # Attempt both independently, including when the driver is unreachable.
        for tag, value in (("HP", "0"), ("HS", "0")):
            try:
                if hasattr(self._driver, "_send_message"):
                    await self._driver._send_message(tag, value)
                elif tag == "HP":
                    await self._driver.set_heat(0)
                else:
                    await self._driver.set_heating_state(False)
            except Exception:
                _logger.warning("Failed to send %s=0 on stop", tag, exc_info=True)

    async def _loop(self) -> None:
        """Background loop to continuously send HS=1 and HP."""
        try:
            while True:
                if (self._preheat_deadline is not None
                        and time.monotonic() >= self._preheat_deadline):
                    self._preheat_deadline = None
                    await self._disable_heat()
                    await self._alert("preheat_timeout", "PREHEAT TIMEOUT - local heating halted. Check Kaleido controls.")
                    return
                await self._send_once()
                await asyncio.sleep(self._send_interval)
        except asyncio.CancelledError:
            return

    async def _connection_ready(self) -> bool:
        generation = getattr(self._driver, "connection_generation", None)
        if not self._driver.is_connected or generation != self._connection_generation:
            if not self._paused_disconnected:
                self._paused_disconnected = True
                _logger.error("Heating paused after disconnect; explicit heat command required")
                await self._alert("heating_disconnected", "Heating paused after disconnect - use Kaleido controls. Send a new heat command to resume.")
        return not self._paused_disconnected

    async def _send_once(self) -> None:
        """Send a single HS=1 and HP command pair."""
        if not await self._connection_ready():
            return
        try:
            # Use _send_message to bypass state caching
            # The Kaleido driver's set_heating_state() caches and may skip sends
            if hasattr(self._driver, "_send_message"):
                await self._driver._send_message("HS", "1")
                if not await self._connection_ready():
                    return
                await self._driver._send_message("HP", str(self._heat_percent))
            else:
                # Fallback for drivers without _send_message (e.g., simulator)
                await self._driver.set_heating_state(True)
                if not await self._connection_ready():
                    return
                await self._driver.set_heat(self._heat_percent)

            self._last_send_time = time.time()
            self._total_sends += 1
            self._consecutive_errors = 0

            _logger.debug(
                "HeatingManager sent HS=1, HP=%d",
                self._heat_percent,
            )

        except Exception:
            self._consecutive_errors += 1
            self._total_errors += 1
            _logger.warning(
                "HeatingManager send error (%d/%d consecutive)",
                self._consecutive_errors,
                self._max_consecutive_errors,
                exc_info=True,
            )

            if self._consecutive_errors >= self._max_consecutive_errors:
                _logger.error(
                    "HeatingManager stopping after %d consecutive errors",
                    self._consecutive_errors,
                )
                if self._task is not None:
                    self._task.cancel()


__all__ = ["HeatingManager", "HeatingStatus"]
