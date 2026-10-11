"""Safety monitor for autonomous roast supervision.

The SafetyMonitor enforces safety thresholds during autonomous roasts.
It subscribes to StateChangeEvents, checks temperature/duration/RoR limits,
triggers emergency stops on critical violations, and includes a watchdog
timer to detect Claude disconnections.

Safety hierarchy:
- EMERGENCY: Automatic action taken (emergency stop)
- CRITICAL: Requires immediate attention
- WARNING: Informational alert, no automatic action

The monitor is event-driven, reacting to StateChangeEvents published by
the RoastMonitor. A separate watchdog task runs independently to detect
when Claude stops calling roast-participation tools.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from maillard.core.events import (
    SafetyAlertEvent,
    SafetyAlertLevel,
    StateChangeEvent,
)
from maillard.roasting.state import RoastPhase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from maillard.core.config import MaillardConfig
    from maillard.core.events import EventBus, UnsubscribeFunc
    from maillard.driver.base import RoasterDriver
    from maillard.roasting.session import RoastSession

_logger = logging.getLogger(__name__)


@dataclass
class SafetyLimits:
    """Safety threshold configuration.

    All temperature values are in Celsius.
    """

    max_bt_temp: float = 230.0
    max_et_temp: float = 240.0  # Observed ET <=200C; 40C alarm headroom on this probe.
    max_ror: float = 20.0
    min_ror: float = 3.0
    max_roast_duration: int = 1200  # seconds
    watchdog_warning_seconds: int = 30
    watchdog_emergency_seconds: int = 60

    @classmethod
    def from_config(cls, config: MaillardConfig) -> SafetyLimits:
        """Create SafetyLimits from MaillardConfig."""
        return cls(
            max_bt_temp=config.max_bt_temp,
            max_et_temp=config.max_et_temp,
            max_ror=config.max_ror,
            min_ror=config.min_ror,
            max_roast_duration=config.max_roast_duration,
            watchdog_warning_seconds=config.watchdog_warning_seconds,
            watchdog_emergency_seconds=config.watchdog_emergency_seconds,
        )


@dataclass
class SafetyAlert:
    """Record of a safety alert."""

    level: SafetyAlertLevel
    condition: str
    message: str
    current_value: float | None = None
    threshold: float | None = None
    action_taken: str | None = None
    timestamp: float = field(default_factory=time.time)


class SafetyMonitor:
    """Monitors roast safety and triggers emergency stops when needed.

    The SafetyMonitor is event-driven: it subscribes to StateChangeEvents
    and checks safety conditions on each event. A separate watchdog task
    monitors for Claude disconnection.

    Usage:
        monitor = SafetyMonitor(driver, event_bus, session, config)
        await monitor.start()
        # ... roast in progress ...
        # Reset watchdog when Claude calls roast tools:
        monitor.reset_watchdog()
        # ...
        await monitor.stop()

    Attributes:
        limits: Safety threshold configuration
        recent_alerts: List of recent safety alerts
    """

    def __init__(
        self,
        driver: RoasterDriver,
        event_bus: EventBus,
        session: RoastSession,
        config: MaillardConfig,
        *,
        watchdog_enforced: bool = True,
        on_emergency: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize the SafetyMonitor.

        Args:
            driver: Roaster driver for emergency stop
            event_bus: Event bus to subscribe to and publish alerts
            session: Active roast session for state access
            config: Configuration with safety limits
            watchdog_enforced: When True (autonomous AI/MCP control), a stale
                watchdog triggers an emergency stop. When False (manual or
                suggest-only roasting with an operator in control), the
                watchdog only publishes a warning.
            on_emergency: Optional coroutine run *before* the driver emergency
                stop. Used to stop components that would otherwise re-apply
                heat (HeatingManager re-sends HS=1/HP every second, execution
                engine, AI auto mode).
        """
        self._driver = driver
        self._event_bus = event_bus
        self._session = session
        self.limits = SafetyLimits.from_config(config)
        self._watchdog_enforced = watchdog_enforced
        self._on_emergency = on_emergency

        # Internal state
        self._unsubscribe: UnsubscribeFunc | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_tool_call_time: float = time.time()
        self._alerted_conditions: set[str] = set()
        self._low_ror_start_time: float | None = None
        self.recent_alerts: list[SafetyAlert] = []
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if the monitor is active."""
        return self._running

    async def start(self) -> None:
        """Start monitoring safety conditions.

        Subscribes to StateChangeEvents and starts the watchdog timer.
        """
        if self._running:
            return

        # Subscribe to state changes
        self._unsubscribe = self._event_bus.on(
            StateChangeEvent, self._on_state_change
        )

        # Start watchdog
        self._last_tool_call_time = time.time()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="safety-watchdog"
        )

        self._running = True
        _logger.info(
            "SafetyMonitor started (limits: BT<%.0f, ET<%.0f, RoR<%.1f, duration<%ds)",
            self.limits.max_bt_temp,
            self.limits.max_et_temp,
            self.limits.max_ror,
            self.limits.max_roast_duration,
        )

    async def stop(self) -> None:
        """Stop monitoring and clean up.

        Unsubscribes from events and cancels the watchdog task.
        """
        if not self._running:
            return

        # Unsubscribe from events
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

        # Cancel watchdog
        if self._watchdog_task is not None:
            task = self._watchdog_task
            self._watchdog_task = None
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Clear state
        self._alerted_conditions.clear()
        self._low_ror_start_time = None
        self._running = False
        _logger.info("SafetyMonitor stopped")

    @property
    def watchdog_enforced(self) -> bool:
        """Whether a stale watchdog triggers an emergency stop."""
        return self._watchdog_enforced

    def set_watchdog_enforced(self, enforced: bool) -> None:
        """Switch watchdog behavior when the control mode changes mid-roast.

        Enabling enforcement resets the timer so switching into autonomous
        mode never trips an immediate emergency stop.
        """
        if enforced and not self._watchdog_enforced:
            self.reset_watchdog()
        self._watchdog_enforced = enforced
        _logger.info(
            "Safety watchdog %s",
            "ENFORCED (emergency stop on timeout)" if enforced else "warn-only (manual mode)",
        )

    def reset_watchdog(self) -> None:
        """Reset the watchdog timer.

        Call this when Claude makes a roast-participation tool call
        to prove it's still actively monitoring.
        """
        self._last_tool_call_time = time.time()
        # Clear watchdog alert if it was set
        self._clear_condition("watchdog_warning")

    def get_status(self) -> dict[str, Any]:
        """Get current safety status.

        Returns:
            Dict with safety limits, watchdog state, and recent alerts
        """
        now = time.time()
        seconds_since_tool_call = now - self._last_tool_call_time

        return {
            "active": self._running,
            "limits": {
                "max_bt_temp": self.limits.max_bt_temp,
                "max_et_temp": self.limits.max_et_temp,
                "max_ror": self.limits.max_ror,
                "min_ror": self.limits.min_ror,
                "max_roast_duration": self.limits.max_roast_duration,
            },
            "watchdog": {
                "seconds_since_last_tool_call": round(seconds_since_tool_call, 1),
                "warning_threshold": self.limits.watchdog_warning_seconds,
                "emergency_threshold": self.limits.watchdog_emergency_seconds,
                "enforced": self._watchdog_enforced,
                "status": self._get_watchdog_status(seconds_since_tool_call),
            },
            "active_alerts": list(self._alerted_conditions),
            "recent_alerts": [
                {
                    "level": a.level.name,
                    "condition": a.condition,
                    "message": a.message,
                    "timestamp": a.timestamp,
                }
                for a in self.recent_alerts[-10:]  # Last 10 alerts
            ],
        }

    def _get_watchdog_status(self, seconds: float) -> str:
        """Get human-readable watchdog status."""
        if seconds >= self.limits.watchdog_emergency_seconds:
            return "EMERGENCY"
        elif seconds >= self.limits.watchdog_warning_seconds:
            return "WARNING"
        else:
            return "OK"

    async def _on_state_change(self, event: StateChangeEvent) -> None:
        """Handle a StateChangeEvent and check safety conditions.

        Args:
            event: The state change event with current roaster state
        """
        state = event.state

        # Check temperature limits
        bt = state.get("BT")
        et = state.get("ET")

        if bt is not None:
            await self._check_bt_limit(bt)

        if et is not None:
            await self._check_et_limit(et)

        # Check duration limit
        elapsed = self._session.elapsed_time
        if elapsed is not None:
            await self._check_duration_limit(elapsed)

        # Check RoR limits
        ror = self._session.latest_ror
        if ror is not None:
            await self._check_ror_limits(ror, bt)

    async def _check_bt_limit(self, bt: float) -> None:
        """Check bean temperature limit."""
        if bt > self.limits.max_bt_temp:
            await self._trigger_emergency(
                condition="bt_over_limit",
                message=f"BT {bt:.1f}°C exceeds limit {self.limits.max_bt_temp:.0f}°C",
                current_value=bt,
                threshold=self.limits.max_bt_temp,
            )
        else:
            self._clear_condition("bt_over_limit")

    async def _check_et_limit(self, et: float) -> None:
        """Check environment temperature limit."""
        if et > self.limits.max_et_temp:
            await self._trigger_emergency(
                condition="et_over_limit",
                message=f"ET {et:.1f}°C exceeds limit {self.limits.max_et_temp:.0f}°C",
                current_value=et,
                threshold=self.limits.max_et_temp,
            )
        else:
            self._clear_condition("et_over_limit")

    async def _check_duration_limit(self, elapsed: float) -> None:
        """Check roast duration limit."""
        if elapsed > self.limits.max_roast_duration:
            await self._trigger_emergency(
                condition="duration_exceeded",
                message=f"Roast duration {elapsed:.0f}s exceeds limit {self.limits.max_roast_duration}s",
                current_value=elapsed,
                threshold=float(self.limits.max_roast_duration),
            )
        else:
            self._clear_condition("duration_exceeded")

    async def _check_ror_limits(self, ror: float, bt: float | None = None) -> None:
        """Check rate of rise limits."""
        # Skip high RoR check during DRYING phase — RoR naturally spikes
        # after charge and around the turning point
        phase = self._session.current_phase
        if phase == RoastPhase.DRYING:
            self._clear_condition("ror_high")
            self._clear_condition("ror_stall")
            self._low_ror_start_time = None
            return

        # High RoR warning (no action)
        if ror > self.limits.max_ror:
            self._publish_warning(
                condition="ror_high",
                message=f"RoR {ror:.1f}°C/min exceeds recommended max {self.limits.max_ror:.1f}°C/min",
                current_value=ror,
                threshold=self.limits.max_ror,
            )
        else:
            self._clear_condition("ror_high")

        # Low RoR stall warning (not during DRYING phase)
        phase = self._session.current_phase
        from maillard.ai.calibration import get_calibration

        c = get_calibration(600)
        minimum = min(self.limits.min_ror, c["min_maillard_ror"])
        if (phase == RoastPhase.MAILLARD and bt is not None
                and c["dry_end_temp"] <= bt < c["fc_start_temp"] - 5
                and ror < minimum):
            # Track how long RoR has been low
            if self._low_ror_start_time is None:
                self._low_ror_start_time = time.time()
            elif time.time() - self._low_ror_start_time > 30:
                # Low RoR for over 30 seconds
                self._publish_warning(
                    condition="ror_stall",
                    message=f"RoR {ror:.1f}°C/min below minimum {minimum:.1f}°C/min for 30s",
                    current_value=ror,
                    threshold=minimum,
                )
        else:
            self._low_ror_start_time = None
            self._clear_condition("ror_stall")

    async def _watchdog_loop(self) -> None:
        """Background task to check for Claude disconnection."""
        try:
            while True:
                await asyncio.sleep(1.0)
                await self._check_watchdog()
        except asyncio.CancelledError:
            return

    async def _check_watchdog(self) -> None:
        """Check watchdog timer and take action if needed."""
        elapsed = time.time() - self._last_tool_call_time

        if not self._watchdog_enforced:
            # Manual / suggest-only: the operator is in control, so a quiet
            # AI is not a hazard. Warn once, never stop the roast.
            if elapsed >= self.limits.watchdog_warning_seconds:
                self._publish_warning(
                    condition="watchdog_warning",
                    message=(
                        f"No AI/MCP heartbeat for {elapsed:.0f}s "
                        "(manual mode - no automatic action)"
                    ),
                    current_value=elapsed,
                    threshold=float(self.limits.watchdog_warning_seconds),
                )
            return

        if elapsed >= self.limits.watchdog_emergency_seconds:
            await self._trigger_emergency(
                condition="watchdog_emergency",
                message=f"No Claude activity for {elapsed:.0f}s - possible disconnection",
                current_value=elapsed,
                threshold=float(self.limits.watchdog_emergency_seconds),
            )
        elif elapsed >= self.limits.watchdog_warning_seconds:
            self._publish_warning(
                condition="watchdog_warning",
                message=f"No Claude activity for {elapsed:.0f}s - monitoring may be interrupted",
                current_value=elapsed,
                threshold=float(self.limits.watchdog_warning_seconds),
            )

    async def _trigger_emergency(
        self,
        condition: str,
        message: str,
        current_value: float | None = None,
        threshold: float | None = None,
    ) -> None:
        """Trigger an emergency stop and publish EMERGENCY alert.

        Args:
            condition: Unique identifier for this condition
            message: Human-readable description
            current_value: The value that triggered the alert
            threshold: The threshold that was exceeded
        """
        # Deduplicate: only alert once per condition
        if condition in self._alerted_conditions:
            return

        self._alerted_conditions.add(condition)

        # Stop anything that would re-apply heat (HeatingManager keep-alive,
        # execution engine, AI auto) BEFORE the driver stop, otherwise the
        # HeatingManager re-sends HS=1/HP=<last> within ~1s.
        if self._on_emergency is not None:
            try:
                await self._on_emergency()
            except Exception:
                _logger.critical(
                    "SAFETY EMERGENCY: on_emergency hook failed", exc_info=True
                )

        # Execute emergency stop
        action_taken: str | None = None
        try:
            if await self._driver.emergency_stop() is not True:
                raise ConnectionError("Heat-off not confirmed")
            action_taken = "Emergency stop executed"
            _logger.critical(
                "SAFETY EMERGENCY: %s - Emergency stop executed", message
            )
        except Exception:
            action_taken = "EMERGENCY STOP COMMAND FAILED - CUT HEAT ON ROASTER"
            message = f"{message}. {action_taken}"
            _logger.critical(
                "SAFETY EMERGENCY: %s - FAILED to execute emergency stop!",
                message,
                exc_info=True,
            )
            # Continue monitoring even if emergency stop failed
            # The condition will be re-checked on next event

        # Belt and braces: explicitly drop the heating state (HS=0).
        set_heating_state = getattr(self._driver, "set_heating_state", None)
        if callable(set_heating_state):
            try:
                result = set_heating_state(False)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _logger.warning("Failed to send HS=0 after emergency stop", exc_info=True)

        level = (SafetyAlertLevel.EMERGENCY if action_taken == "Emergency stop executed"
                 else SafetyAlertLevel.CRITICAL)
        # Record and publish alert
        alert = SafetyAlert(
            level=level,
            condition=condition,
            message=message,
            current_value=current_value,
            threshold=threshold,
            action_taken=action_taken,
        )
        self.recent_alerts.append(alert)

        await self._event_bus.publish(
            SafetyAlertEvent(
                level=level,
                message=message,
                condition="emergency_stop_failed" if level == SafetyAlertLevel.CRITICAL else None,
                current_value=current_value,
                threshold=threshold,
                action_taken=action_taken,
            )
        )

    def _publish_warning(
        self,
        condition: str,
        message: str,
        current_value: float | None = None,
        threshold: float | None = None,
    ) -> None:
        """Publish a WARNING alert (no automatic action).

        Args:
            condition: Unique identifier for this condition
            message: Human-readable description
            current_value: The value that triggered the alert
            threshold: The threshold that was exceeded
        """
        # Deduplicate: only alert once per condition
        if condition in self._alerted_conditions:
            return

        self._alerted_conditions.add(condition)
        _logger.warning("SAFETY WARNING: %s", message)

        alert = SafetyAlert(
            level=SafetyAlertLevel.WARNING,
            condition=condition,
            message=message,
            current_value=current_value,
            threshold=threshold,
        )
        self.recent_alerts.append(alert)

        # Publish async - use create_task since we're in a sync method
        asyncio.create_task(
            self._event_bus.publish(
                SafetyAlertEvent(
                    level=SafetyAlertLevel.WARNING,
                    message=message,
                    current_value=current_value,
                    threshold=threshold,
                )
            )
        )

    def _clear_condition(self, condition: str) -> None:
        """Clear an alerted condition when values return to safe range."""
        self._alerted_conditions.discard(condition)


__all__ = ["SafetyLimits", "SafetyMonitor", "SafetyAlert"]
