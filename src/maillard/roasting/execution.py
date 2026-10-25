"""Roast execution engine for autonomous profile following.

The RoastExecutionEngine decouples tight control loops from Claude's response
latency by running a server-side loop that follows roast profiles at 1.5s
intervals. It subscribes to StateChangeEvents and adjusts controls based on
the current profile phase.

Modes:
- PROFILE: Auto-follow profile phases (heat, fan, drum interpolation)
- MANUAL: Claude controls; engine monitors only and suggests events
- HOLD: Maintain current settings

The engine publishes suggestion events (TurningPointSuggested, DryEndSuggested,
FirstCrackSuggested, DropSuggested) for Claude to confirm and mark. It never
auto-marks events or auto-drops - those require explicit Claude confirmation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from maillard.core.events import (
    DropSuggested,
    DryEndSuggested,
    FirstCrackSuggested,
    StateChangeEvent,
    TurningPointSuggested,
)

if TYPE_CHECKING:
    from maillard.core.events import EventBus, UnsubscribeFunc
    from maillard.db.schemas import PhaseSchema
    from maillard.driver.base import RoasterDriver
    from maillard.roasting.session import RoastSession

_logger = logging.getLogger(__name__)


class ControlMode(Enum):
    """Control mode for the execution engine."""

    PROFILE = "profile"  # Auto-follow profile phases
    MANUAL = "manual"  # Claude controls; engine monitors only
    HOLD = "hold"  # Maintain current settings


@dataclass
class ExecutionPhase:
    """Current execution phase information.

    Tracks the current and next profile phases along with interpolated targets.
    """

    current_phase_name: str
    current_phase_index: int
    next_phase_name: str | None
    next_phase_index: int | None
    progress_in_phase: float  # 0.0 to 1.0
    target_heat: int
    target_fan: int
    target_drum: int
    target_temp: float
    time_to_next_phase: float | None  # seconds until next phase


@dataclass
class TargetSettings:
    """Interpolated control settings for current moment."""

    heat: int
    fan: int
    drum: int
    target_temp: float


class RoastExecutionEngine:
    """Execution engine that follows roast profiles autonomously.

    The engine subscribes to StateChangeEvents and:
    - In PROFILE mode: interpolates control settings between phases
    - In all modes: detects event suggestions and publishes them

    Usage:
        engine = RoastExecutionEngine(
            driver=driver,
            event_bus=event_bus,
            session=session,
            profile_phases=phases,
        )
        await engine.start()
        # ... roast in progress ...
        await engine.stop()
    """

    def __init__(
        self,
        driver: RoasterDriver,
        event_bus: EventBus,
        session: RoastSession,
        profile_phases: list[PhaseSchema],
        *,
        initial_mode: ControlMode = ControlMode.PROFILE,
        dry_end_threshold: float = 150.0,
        fc_threshold: float = 195.0,
        turning_point_confirmation_samples: int = 3,
        drop_target_temp: float | None = None,
    ) -> None:
        """Initialize the execution engine.

        Args:
            driver: Roaster driver for control commands
            event_bus: Event bus for subscribing and publishing
            session: Active roast session for state access
            profile_phases: List of PhaseSchema from the profile
            initial_mode: Starting control mode (default: PROFILE)
            dry_end_threshold: Temperature threshold for dry end suggestion
            fc_threshold: Temperature threshold for first crack suggestion
            turning_point_confirmation_samples: Samples of rising BT to confirm TP
        """
        self._driver = driver
        self._event_bus = event_bus
        self._session = session
        self._phases = profile_phases
        self._mode = initial_mode
        self._dry_end_threshold = dry_end_threshold
        self._fc_threshold = fc_threshold
        self._tp_confirmation_samples = turning_point_confirmation_samples
        self._drop_target_temp = drop_target_temp

        # Internal state
        self._unsubscribe: UnsubscribeFunc | None = None
        self._running = False

        # Event detection state
        self._min_bt: float | None = None
        self._min_bt_time: float | None = None
        self._bt_history: list[tuple[float, float]] = []  # (time, bt) pairs
        self._suggested_events: set[str] = set()  # Track which events already suggested

        # Phase tracking
        self._current_phase_index = 0
        self._last_applied_settings: TargetSettings | None = None

    @property
    def mode(self) -> ControlMode:
        """Current control mode."""
        return self._mode

    @property
    def is_running(self) -> bool:
        """Whether the engine is active."""
        return self._running

    async def start(self) -> None:
        """Start the execution engine.

        Subscribes to StateChangeEvents and begins profile following
        (if in PROFILE mode) and event detection.
        """
        if self._running:
            return

        self._unsubscribe = self._event_bus.on(
            StateChangeEvent, self._on_state_change
        )
        self._running = True
        _logger.info(
            "RoastExecutionEngine started (mode=%s, phases=%d)",
            self._mode.value,
            len(self._phases),
        )

    async def stop(self) -> None:
        """Stop the execution engine.

        Unsubscribes from events and clears internal state.
        """
        if not self._running:
            return

        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

        self._running = False
        _logger.info("RoastExecutionEngine stopped")

    def switch_mode(self, mode: ControlMode) -> None:
        """Switch control mode.

        Args:
            mode: New control mode
        """
        old_mode = self._mode
        self._mode = mode
        _logger.info("Control mode switched: %s -> %s", old_mode.value, mode.value)

    def notify_manual_override(self, control: str, value: int) -> None:
        """Notify engine of manual control override.

        When Claude calls set_heat/set_fan/set_drum during PROFILE mode,
        this method auto-switches to MANUAL mode.

        Args:
            control: Control name ("heat", "fan", "drum")
            value: New value
        """
        if self._mode == ControlMode.PROFILE:
            _logger.info(
                "Manual override detected (%s=%d), switching to MANUAL mode",
                control,
                value,
            )
            self._mode = ControlMode.MANUAL

    def get_status(self) -> dict[str, Any]:
        """Get current execution status.

        Returns:
            Dict with mode, phase info, targets, and suggestions
        """
        elapsed = self._session.elapsed_time or 0.0
        phase_info = self._get_current_phase_info(elapsed)

        status: dict[str, Any] = {
            "running": self._running,
            "mode": self._mode.value,
            "elapsed_seconds": round(elapsed, 1),
            "phase_count": len(self._phases),
        }

        if phase_info:
            status["current_phase"] = {
                "name": phase_info.current_phase_name,
                "index": phase_info.current_phase_index,
                "progress": round(phase_info.progress_in_phase, 2),
            }
            if phase_info.next_phase_name:
                status["next_phase"] = {
                    "name": phase_info.next_phase_name,
                    "index": phase_info.next_phase_index,
                    "seconds_until": round(phase_info.time_to_next_phase or 0, 1),
                }
            status["targets"] = {
                "heat_percent": phase_info.target_heat,
                "fan_percent": phase_info.target_fan,
                "drum_percent": phase_info.target_drum,
                "target_temp": round(phase_info.target_temp, 1),
            }

        if self._last_applied_settings:
            status["last_applied"] = {
                "heat_percent": self._last_applied_settings.heat,
                "fan_percent": self._last_applied_settings.fan,
                "drum_percent": self._last_applied_settings.drum,
            }

        status["suggested_events"] = list(self._suggested_events)
        status["turning_point_detection"] = {
            "min_bt": round(self._min_bt, 1) if self._min_bt else None,
            "min_bt_time": round(self._min_bt_time, 1) if self._min_bt_time else None,
            "bt_samples": len(self._bt_history),
        }

        return status

    async def _on_state_change(self, event: StateChangeEvent) -> None:
        """Handle StateChangeEvent: adjust controls and check for suggestions.

        Args:
            event: State change event with current roaster state
        """
        if not self._running:
            return

        state = event.state
        bt = state.get("BT")
        elapsed = self._session.elapsed_time

        if bt is None or elapsed is None:
            return

        # Track BT history for turning point detection
        self._track_bt(elapsed, bt)

        # Check for event suggestions (all modes)
        await self._check_event_suggestions(bt, elapsed)

        # Apply control settings (PROFILE mode only)
        if self._mode == ControlMode.PROFILE:
            await self._apply_profile_controls(elapsed)

    def _track_bt(self, elapsed: float, bt: float) -> None:
        """Track BT values for turning point detection.

        Args:
            elapsed: Time since charge in seconds
            bt: Current bean temperature
        """
        self._bt_history.append((elapsed, bt))

        # Keep only recent samples (last 60 seconds)
        cutoff = elapsed - 60.0
        self._bt_history = [(t, b) for t, b in self._bt_history if t >= cutoff]

        # Track minimum BT
        if self._min_bt is None or bt < self._min_bt:
            self._min_bt = bt
            self._min_bt_time = elapsed

    async def _check_event_suggestions(self, bt: float, elapsed: float) -> None:
        """Check for events to suggest and publish suggestion events.

        Args:
            bt: Current bean temperature
            elapsed: Time since charge in seconds
        """
        # Turning point detection (after initial cool-down, BT rises)
        if "turning_point" not in self._suggested_events:
            if self._detect_turning_point(bt, elapsed):
                await self._event_bus.publish(
                    TurningPointSuggested(
                        temp_bt=self._min_bt or bt,
                        elapsed_seconds=self._min_bt_time or elapsed,
                        min_bt_detected=self._min_bt or bt,
                        confidence=0.8,
                    )
                )
                self._suggested_events.add("turning_point")
                _logger.info(
                    "Turning point suggested at %.1f°C (%.1fs)",
                    self._min_bt or bt,
                    self._min_bt_time or elapsed,
                )

        # Dry end suggestion (approaching threshold)
        if "dry_end" not in self._suggested_events:
            if bt >= self._dry_end_threshold - 5.0:  # Suggest 5°C before threshold
                await self._event_bus.publish(
                    DryEndSuggested(
                        temp_bt=bt,
                        elapsed_seconds=elapsed,
                        threshold_temp=self._dry_end_threshold,
                    )
                )
                self._suggested_events.add("dry_end")
                _logger.info(
                    "Dry end suggested at %.1f°C (%.1fs)",
                    bt,
                    elapsed,
                )

        # First crack suggestion (approaching threshold)
        if "fc_start" not in self._suggested_events:
            if bt >= self._fc_threshold - 5.0:  # Suggest 5°C before threshold
                await self._event_bus.publish(
                    FirstCrackSuggested(
                        temp_bt=bt,
                        elapsed_seconds=elapsed,
                        threshold_temp=self._fc_threshold,
                    )
                )
                self._suggested_events.add("fc_start")
                _logger.info(
                    "First crack suggested at %.1f°C (%.1fs)",
                    bt,
                    elapsed,
                )

        # Drop suggestion (calibrated level target, else the profile waypoint)
        if "drop" not in self._suggested_events:
            target_temp = self._drop_target_temp
            target_time = None
            if self._phases:
                last_phase = self._phases[-1]
                target_time = last_phase.target_time_seconds
                if target_temp is None:
                    target_temp = last_phase.target_temp
            if target_temp is not None and (
                bt >= target_temp or (target_time is not None and elapsed >= target_time)
            ):
                await self._event_bus.publish(
                    DropSuggested(
                        temp_bt=bt,
                        elapsed_seconds=elapsed,
                        target_temp=target_temp,
                        target_time=float(target_time) if target_time is not None else None,
                        reason=(
                            "drop target reached"
                            if self._drop_target_temp is not None
                            else "profile target reached"
                        ),
                    )
                )
                self._suggested_events.add("drop")
                _logger.info(
                    "Drop suggested at %.1f°C (%.1fs) - target: %.1f°C",
                    bt,
                    elapsed,
                    target_temp,
                )

    def _detect_turning_point(self, bt: float, elapsed: float) -> bool:
        """Detect if turning point has occurred.

        Turning point is when BT starts rising after reaching minimum.
        We confirm by checking for consistent rising trend.

        Args:
            bt: Current bean temperature
            elapsed: Time since charge

        Returns:
            True if turning point detected
        """
        if self._min_bt is None or self._min_bt_time is None:
            return False

        # Need minimum time after charge (at least 30 seconds)
        if elapsed < 30.0:
            return False

        # BT must be rising above minimum
        if bt <= self._min_bt:
            return False

        # Check for consistent rising trend
        rising_samples = 0
        for i in range(len(self._bt_history) - 1):
            if self._bt_history[i + 1][1] > self._bt_history[i][1]:
                rising_samples += 1

        # Require several consecutive rising samples
        recent_samples = self._bt_history[-self._tp_confirmation_samples:]
        if len(recent_samples) < self._tp_confirmation_samples:
            return False

        # Check last N samples are all rising
        all_rising = all(
            recent_samples[i + 1][1] > recent_samples[i][1]
            for i in range(len(recent_samples) - 1)
        )

        return all_rising and (bt - self._min_bt) >= 2.0  # At least 2°C above minimum

    async def _apply_profile_controls(self, elapsed: float) -> None:
        """Apply interpolated control settings from profile.

        Args:
            elapsed: Time since charge in seconds
        """
        targets = self._calculate_targets(elapsed)
        if targets is None:
            return

        # Only apply if different from last settings
        if self._last_applied_settings:
            if (
                targets.heat == self._last_applied_settings.heat
                and targets.fan == self._last_applied_settings.fan
                and targets.drum == self._last_applied_settings.drum
            ):
                return

        # Apply settings
        try:
            # Enable/disable heating state based on heat level
            # HS must be 1 for the heater to produce heat
            if hasattr(self._driver, "set_heating_state"):
                await self._driver.set_heating_state(targets.heat > 0)

            await self._driver.set_heat(targets.heat)
            await self._driver.set_fan(targets.fan)
            await self._driver.set_drum(targets.drum)
            self._last_applied_settings = targets
            _logger.debug(
                "Applied controls: heat=%d%%, fan=%d%%, drum=%d%%",
                targets.heat,
                targets.fan,
                targets.drum,
            )
        except Exception:
            _logger.warning("Failed to apply profile controls", exc_info=True)

    def _calculate_targets(self, elapsed: float) -> TargetSettings | None:
        """Calculate interpolated target settings for current time.

        Args:
            elapsed: Time since charge in seconds

        Returns:
            TargetSettings with interpolated values, or None if no phases
        """
        if not self._phases:
            return None

        # Find current phase (last phase with target_time <= elapsed)
        current_idx = 0
        for i, phase in enumerate(self._phases):
            if phase.target_time_seconds <= elapsed:
                current_idx = i
            else:
                break

        current_phase = self._phases[current_idx]

        # If at or past last phase, use its settings
        if current_idx >= len(self._phases) - 1:
            return TargetSettings(
                heat=current_phase.heat_percent,
                fan=current_phase.fan_percent,
                drum=current_phase.drum_percent,
                target_temp=current_phase.target_temp,
            )

        # Interpolate between current and next phase
        next_phase = self._phases[current_idx + 1]
        current_time = current_phase.target_time_seconds
        next_time = next_phase.target_time_seconds
        duration = next_time - current_time

        if duration <= 0:
            return TargetSettings(
                heat=current_phase.heat_percent,
                fan=current_phase.fan_percent,
                drum=current_phase.drum_percent,
                target_temp=current_phase.target_temp,
            )

        # Calculate progress through current phase (0.0 to 1.0)
        progress = (elapsed - current_time) / duration
        progress = max(0.0, min(1.0, progress))

        # Linear interpolation
        def lerp(a: float, b: float, t: float) -> float:
            return a + (b - a) * t

        return TargetSettings(
            heat=int(round(lerp(current_phase.heat_percent, next_phase.heat_percent, progress))),
            fan=int(round(lerp(current_phase.fan_percent, next_phase.fan_percent, progress))),
            drum=int(round(lerp(current_phase.drum_percent, next_phase.drum_percent, progress))),
            target_temp=lerp(current_phase.target_temp, next_phase.target_temp, progress),
        )

    def _get_current_phase_info(self, elapsed: float) -> ExecutionPhase | None:
        """Get detailed information about current execution phase.

        Args:
            elapsed: Time since charge in seconds

        Returns:
            ExecutionPhase with current state, or None if no phases
        """
        if not self._phases:
            return None

        # Find current phase
        current_idx = 0
        for i, phase in enumerate(self._phases):
            if phase.target_time_seconds <= elapsed:
                current_idx = i
            else:
                break

        current_phase = self._phases[current_idx]
        targets = self._calculate_targets(elapsed)

        if targets is None:
            return None

        # Calculate progress and next phase info
        if current_idx >= len(self._phases) - 1:
            return ExecutionPhase(
                current_phase_name=current_phase.name,
                current_phase_index=current_idx,
                next_phase_name=None,
                next_phase_index=None,
                progress_in_phase=1.0,
                target_heat=targets.heat,
                target_fan=targets.fan,
                target_drum=targets.drum,
                target_temp=targets.target_temp,
                time_to_next_phase=None,
            )

        next_phase = self._phases[current_idx + 1]
        current_time = current_phase.target_time_seconds
        next_time = next_phase.target_time_seconds
        duration = next_time - current_time

        progress = (elapsed - current_time) / duration if duration > 0 else 1.0
        progress = max(0.0, min(1.0, progress))
        time_to_next = next_time - elapsed

        return ExecutionPhase(
            current_phase_name=current_phase.name,
            current_phase_index=current_idx,
            next_phase_name=next_phase.name,
            next_phase_index=current_idx + 1,
            progress_in_phase=progress,
            target_heat=targets.heat,
            target_fan=targets.fan,
            target_drum=targets.drum,
            target_temp=targets.target_temp,
            time_to_next_phase=time_to_next,
        )

    def mark_event_confirmed(self, event_type: str) -> None:
        """Record that an event has been confirmed by Claude.

        This prevents re-suggesting the same event.

        Args:
            event_type: Event type (turning_point, dry_end, fc_start, drop)
        """
        self._suggested_events.add(event_type)
        _logger.debug("Event confirmed: %s", event_type)


__all__ = [
    "ControlMode",
    "ExecutionPhase",
    "RoastExecutionEngine",
    "TargetSettings",
]
