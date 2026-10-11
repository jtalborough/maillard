"""Software PID controller for roaster heat control.

Provides a PID (Proportional-Integral-Derivative) controller that can
be used to automatically adjust heat output to follow a target temperature
profile. This is used when the roaster's internal PID is unavailable or
insufficient.

Typical coffee roasting PID tuning:
- Kp: 2.0-5.0 (proportional gain)
- Ki: 0.01-0.05 (integral gain, low to avoid overshoot)
- Kd: 1.0-3.0 (derivative gain for RoR control)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)


@dataclass
class PIDConfig:
    """Configuration for PID controller.

    Attributes:
        kp: Proportional gain - how aggressively to respond to error
        ki: Integral gain - how much to accumulate past errors
        kd: Derivative gain - how much to dampen based on rate of change
        output_min: Minimum output value (typically 0%)
        output_max: Maximum output value (typically 100%)
        integral_max: Maximum integral accumulation (anti-windup)
        sample_time: Minimum time between updates in seconds
    """

    kp: float = 3.0
    ki: float = 0.02
    kd: float = 2.0
    output_min: float = 0.0
    output_max: float = 100.0
    integral_max: float = 50.0  # Anti-windup limit
    sample_time: float = 1.0


@dataclass
class PIDState:
    """Internal state of PID controller."""

    setpoint: float = 0.0
    last_input: float | None = None
    last_time: float | None = None
    integral: float = 0.0
    last_error: float = 0.0
    last_output: float = 0.0


class PIDController:
    """PID controller for roaster heat management.

    This controller implements a standard PID algorithm with:
    - Anti-windup protection (integral clamping)
    - Derivative on measurement (not error) to avoid derivative kick
    - Output clamping to valid range
    - Configurable sample time

    Usage:
        pid = PIDController(config=PIDConfig(kp=3.0, ki=0.02, kd=2.0))
        pid.setpoint = 200.0  # Target temperature

        # In control loop:
        heat_percent = pid.compute(current_bt)
        await driver.set_heat(int(heat_percent))

    Tuning Guidelines for Coffee Roasting:
        - Start with Kp=3.0, Ki=0.02, Kd=2.0
        - If oscillating: reduce Kp, increase Kd
        - If too slow to reach target: increase Kp
        - If overshooting: reduce Ki, increase Kd
        - If steady-state error: increase Ki slightly
    """

    def __init__(self, config: PIDConfig | None = None) -> None:
        """Initialize PID controller.

        Args:
            config: PID configuration. Uses defaults if not provided.
        """
        self.config = config or PIDConfig()
        self._state = PIDState()
        self._enabled = False

    @property
    def setpoint(self) -> float:
        """Get current setpoint (target temperature)."""
        return self._state.setpoint

    @setpoint.setter
    def setpoint(self, value: float) -> None:
        """Set target temperature."""
        self._state.setpoint = value

    @property
    def enabled(self) -> bool:
        """Check if PID is enabled."""
        return self._enabled

    def enable(self) -> None:
        """Enable the PID controller."""
        self._enabled = True
        _logger.info("PID controller enabled (setpoint=%.1f)", self._state.setpoint)

    def disable(self) -> None:
        """Disable the PID controller."""
        self._enabled = False
        _logger.info("PID controller disabled")

    def reset(self) -> None:
        """Reset PID state.

        Call this when starting a new roast or changing setpoint significantly.
        """
        self._state = PIDState(setpoint=self._state.setpoint)
        _logger.debug("PID controller reset")

    def compute(self, current_value: float) -> float:
        """Compute PID output based on current measurement.

        Args:
            current_value: Current measured value (e.g., BT in °C)

        Returns:
            Control output (heat percentage, 0-100)
        """
        if not self._enabled:
            return self._state.last_output

        now = time.monotonic()
        config = self.config
        state = self._state

        # Check sample time
        if state.last_time is not None:
            dt = now - state.last_time
            if dt < config.sample_time:
                return state.last_output
        else:
            dt = config.sample_time

        # Calculate error
        error = state.setpoint - current_value

        # Proportional term
        p_term = config.kp * error

        # Integral term with anti-windup
        state.integral += error * dt
        state.integral = max(-config.integral_max, min(config.integral_max, state.integral))
        i_term = config.ki * state.integral

        # Derivative term (on measurement to avoid derivative kick)
        if state.last_input is not None:
            d_input = (current_value - state.last_input) / dt
            d_term = -config.kd * d_input  # Negative because we want to counteract change
        else:
            d_term = 0.0

        # Calculate output
        output = p_term + i_term + d_term

        # Clamp output
        output = max(config.output_min, min(config.output_max, output))

        # Update state
        state.last_input = current_value
        state.last_time = now
        state.last_error = error
        state.last_output = output

        _logger.debug(
            "PID: SP=%.1f PV=%.1f err=%.1f P=%.1f I=%.1f D=%.1f out=%.1f",
            state.setpoint,
            current_value,
            error,
            p_term,
            i_term,
            d_term,
            output,
        )

        return output

    def get_status(self) -> dict:
        """Get current PID status for monitoring.

        Returns:
            Dict with setpoint, enabled state, last output, integral term
        """
        return {
            "enabled": self._enabled,
            "setpoint": self._state.setpoint,
            "last_output": self._state.last_output,
            "integral": self._state.integral,
            "last_error": self._state.last_error,
            "config": {
                "kp": self.config.kp,
                "ki": self.config.ki,
                "kd": self.config.kd,
            },
        }


class RoastPIDController(PIDController):
    """PID controller with roast-phase-aware tuning.

    Extends the base PID controller with automatic tuning adjustments
    based on the current roast phase. Different phases benefit from
    different PID parameters:

    - PREHEAT: Aggressive heating to reach charge temp
    - DRYING: Moderate, steady heat application
    - MAILLARD: Careful control as reactions begin
    - DEVELOPMENT: Precise control for flavor development
    - COOLING: Not PID controlled (fan only)
    """

    # Phase-specific tuning multipliers (relative to base config)
    PHASE_TUNING = {
        "PREHEAT": {"kp": 1.5, "ki": 0.5, "kd": 0.5},  # Aggressive
        "DRYING": {"kp": 1.0, "ki": 1.0, "kd": 1.0},   # Balanced
        "MAILLARD": {"kp": 0.8, "ki": 1.2, "kd": 1.5}, # Careful
        "DEVELOPMENT": {"kp": 0.6, "ki": 1.0, "kd": 2.0},  # Precise
    }

    def __init__(self, config: PIDConfig | None = None) -> None:
        """Initialize roast PID controller."""
        super().__init__(config)
        self._base_config = PIDConfig(
            kp=self.config.kp,
            ki=self.config.ki,
            kd=self.config.kd,
            output_min=self.config.output_min,
            output_max=self.config.output_max,
            integral_max=self.config.integral_max,
            sample_time=self.config.sample_time,
        )
        self._current_phase: str | None = None

    def set_phase(self, phase: str) -> None:
        """Update PID tuning based on roast phase.

        Args:
            phase: Current roast phase name (PREHEAT, DRYING, MAILLARD, DEVELOPMENT)
        """
        if phase == self._current_phase:
            return

        self._current_phase = phase
        tuning = self.PHASE_TUNING.get(phase, {"kp": 1.0, "ki": 1.0, "kd": 1.0})

        self.config.kp = self._base_config.kp * tuning["kp"]
        self.config.ki = self._base_config.ki * tuning["ki"]
        self.config.kd = self._base_config.kd * tuning["kd"]

        _logger.info(
            "PID tuning adjusted for %s phase: Kp=%.2f Ki=%.3f Kd=%.2f",
            phase,
            self.config.kp,
            self.config.ki,
            self.config.kd,
        )


__all__ = [
    "PIDConfig",
    "PIDController",
    "RoastPIDController",
]
