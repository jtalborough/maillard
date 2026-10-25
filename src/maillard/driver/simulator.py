"""Simulator driver for offline development and testing.

This module provides a simulated Kaleido M10 roaster for testing
without physical hardware. It includes:
- Realistic thermal model (heating, cooling, thermal mass)
- Auto-detection of turning point
- Configurable first crack trigger temperature
- Network delay and failure injection for testing
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maillard.driver.base import KaleidoState, RoasterDriver, RoastEvent

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


@dataclass
class ThermalModelConfig:
    """Configuration for the thermal model simulation.

    Attributes:
        ambient_temp: Room temperature (°C)
        initial_bt: Bean temperature at start (°C)
        initial_et: Environment temperature at start (°C)
        max_heat_rate: Maximum heating rate at 100% power (°C/min)
        max_cool_rate: Maximum cooling rate at 100% fan (°C/min)
        thermal_mass: Thermal inertia factor (higher = slower response)
        heat_transfer_efficiency: How efficiently heat transfers to beans (0-1)
        fc_trigger_temp: Temperature to auto-trigger first crack indicator (°C)
        tp_detection_enabled: Whether to auto-detect turning point
    """

    ambient_temp: float = 22.0
    initial_bt: float = 22.0
    initial_et: float = 22.0
    max_heat_rate: float = 15.0  # °C/min at 100% heat
    max_cool_rate: float = 5.0  # °C/min at 100% fan
    thermal_mass: float = 1.0  # Inertia factor
    heat_transfer_efficiency: float = 0.7
    fc_trigger_temp: float = 196.0
    tp_detection_enabled: bool = True


@dataclass
class NetworkSimConfig:
    """Configuration for network simulation.

    Attributes:
        enabled: Whether to enable network delay simulation
        min_delay_ms: Minimum network delay in milliseconds
        max_delay_ms: Maximum network delay in milliseconds
        failure_rate: Probability of command failure (0-1)
        disconnect_after_seconds: Trigger disconnection after this many seconds (0 = disabled)
    """

    enabled: bool = False
    min_delay_ms: int = 10
    max_delay_ms: int = 100
    failure_rate: float = 0.0
    disconnect_after_seconds: float = 0.0


@dataclass
class SimulatorState:
    """Internal state for the simulator.

    Tracks temperatures, control settings, and roast events.
    """

    # Temperatures (°C)
    bt: float = 22.0
    et: float = 22.0
    at: float = 22.0  # Ambient

    # Control percentages (0-100)
    hp: int = 0  # Heat power
    fc: int = 0  # Fan speed
    rc: int = 50  # Drum speed (default 50%)

    # PID
    ts: float = 0.0  # Target temperature
    ah: int = 0  # Auto-heat mode

    # Status
    sid: int = 1  # Device status
    hs: int = 0  # Heating state
    ev: int = 0  # Last event
    cs: int = 0  # Command state

    # String identifiers
    tu: str = "C"  # Temperature unit
    sn: str = "SIM-001"  # Serial number
    sc: str = ""  # Safety control response
    cl: str = ""  # Clear guard response

    # Roast tracking (for turning point detection)
    roast_started: bool = False
    turning_point_detected: bool = False
    fc_triggered: bool = False
    min_bt_after_charge: float = 999.0
    last_bt: float = 22.0

    # Timing
    last_update: float = field(default_factory=time.monotonic)
    connect_time: float = 0.0


class SimulatorDriver(RoasterDriver):
    """Simulated roaster driver for offline development and testing.

    This driver implements the RoasterDriver protocol using a thermal
    simulation model. It supports:
    - Realistic temperature curves based on heat/fan settings
    - Auto-detection of turning point after charge
    - Configurable first crack trigger temperature
    - Network delay and failure injection for resilience testing

    Usage:
        # Basic usage
        driver = SimulatorDriver()
        await driver.connect("simulator", 0)  # host/port ignored

        state = await driver.read_state()
        print(f"BT: {state.get('BT')}")

        await driver.set_heat(80)
        await asyncio.sleep(1)
        state = await driver.read_state()  # BT will have increased

        await driver.disconnect()

        # With custom thermal model
        thermal_config = ThermalModelConfig(
            max_heat_rate=20.0,  # Faster heating
            fc_trigger_temp=198.0,  # Higher FC trigger
        )
        driver = SimulatorDriver(thermal_config=thermal_config)

        # With network simulation
        network_config = NetworkSimConfig(
            enabled=True,
            min_delay_ms=50,
            max_delay_ms=200,
            failure_rate=0.05,  # 5% command failure
        )
        driver = SimulatorDriver(network_config=network_config)
    """

    def __init__(
        self,
        thermal_config: ThermalModelConfig | None = None,
        network_config: NetworkSimConfig | None = None,
        *,
        connected_callback: Callable[[], None] | None = None,
        disconnected_callback: Callable[[], None] | None = None,
        drop_event_code: int = 8,
    ) -> None:
        """Initialize the simulator driver.

        Args:
            thermal_config: Optional thermal model configuration
            network_config: Optional network simulation configuration
            connected_callback: Called when connection is established
            disconnected_callback: Called when disconnected
        """
        self._thermal_config = thermal_config or ThermalModelConfig()
        self._network_config = network_config or NetworkSimConfig()
        self._connected_callback = connected_callback
        self._disconnected_callback = disconnected_callback
        self.drop_event_code = int(drop_event_code)

        self._is_connected = False
        self.connection_generation = 0
        self._connection_listeners: list[Callable[[dict], None]] = []
        self._sim_state = SimulatorState(
            bt=self._thermal_config.initial_bt,
            et=self._thermal_config.initial_et,
            at=self._thermal_config.ambient_temp,
        )

        # Background task for continuous simulation
        self._simulation_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def is_connected(self) -> bool:
        """Check if the driver is connected."""
        return self._is_connected

    @property
    def state(self) -> KaleidoState:
        """Get the current roaster state snapshot."""
        self._update_thermal_model()
        return self._build_state()

    def _build_state(self) -> KaleidoState:
        """Build a KaleidoState dict from internal state."""
        return KaleidoState(
            sid=self._sim_state.sid,
            TU=self._sim_state.tu,
            BT=round(self._sim_state.bt, 1),
            ET=round(self._sim_state.et, 1),
            AT=round(self._sim_state.at, 1),
            TS=round(self._sim_state.ts, 1),
            HP=self._sim_state.hp,
            FC=self._sim_state.fc,
            RC=self._sim_state.rc,
            AH=self._sim_state.ah,
            HS=self._sim_state.hs,
            EV=self._sim_state.ev,
            CS=self._sim_state.cs,
            SN=self._sim_state.sn,
            SC=self._sim_state.sc,
            CL=self._sim_state.cl,
        )

    # -------------------------------------------------------------------------
    # Thermal Model
    # -------------------------------------------------------------------------

    def _update_thermal_model(self) -> None:
        """Update temperatures based on current settings and elapsed time.

        The thermal model simulates:
        1. Heat input from heating element (proportional to HP%)
        2. Heat loss from fan cooling (proportional to FC%)
        3. Thermal inertia (temperature changes are gradual)
        4. Environment temperature tracks above bean temp
        """
        current_time = time.monotonic()
        dt_seconds = current_time - self._sim_state.last_update
        self._sim_state.last_update = current_time

        if dt_seconds <= 0 or dt_seconds > 10:  # Cap at 10 seconds
            return

        dt_minutes = dt_seconds / 60.0
        config = self._thermal_config

        # Calculate heat input (°C/min at current heat percentage)
        # Heat only applies if heating state (HS) is enabled
        effective_hp = self._sim_state.hp if self._sim_state.hs == 1 else 0
        heat_input = (effective_hp / 100.0) * config.max_heat_rate

        # Calculate cooling (°C/min at current fan percentage)
        # Cooling is proportional to temperature difference from ambient
        temp_diff = self._sim_state.bt - config.ambient_temp
        cooling_factor = max(0, temp_diff / 200.0)  # Normalize by max temp diff
        fan_cooling = (self._sim_state.fc / 100.0) * config.max_cool_rate * cooling_factor

        # Natural heat loss (always some cooling towards ambient)
        natural_cooling = 0.5 * cooling_factor  # Small constant loss

        # Net temperature change
        net_rate = heat_input - fan_cooling - natural_cooling

        # Apply thermal mass (inertia)
        effective_rate = net_rate / config.thermal_mass

        # Update bean temperature
        self._sim_state.last_bt = self._sim_state.bt
        self._sim_state.bt += effective_rate * dt_minutes

        # Clamp BT to reasonable range
        self._sim_state.bt = max(config.ambient_temp - 5, min(280.0, self._sim_state.bt))

        # ET typically runs 20-40°C above BT during roast
        et_offset = 25.0 + (self._sim_state.hp / 100.0) * 15.0
        target_et = self._sim_state.bt + et_offset
        # ET changes more slowly than BT
        self._sim_state.et += (target_et - self._sim_state.et) * 0.3 * dt_minutes
        self._sim_state.et = max(config.ambient_temp, min(300.0, self._sim_state.et))

        # Note: HS (heating state) is now controlled explicitly via set_heating_state
        # and is not automatically updated based on HP

        # Auto-detect turning point
        if self._thermal_config.tp_detection_enabled:
            self._check_turning_point()

        # Check for first crack trigger
        self._check_first_crack()

    def _check_turning_point(self) -> None:
        """Check if turning point has been reached after charge.

        Turning point is the lowest BT after charge, detected when
        temperature starts rising again.
        """
        if not self._sim_state.roast_started:
            return
        if self._sim_state.turning_point_detected:
            return

        # Track minimum temperature after charge
        if self._sim_state.bt < self._sim_state.min_bt_after_charge:
            self._sim_state.min_bt_after_charge = self._sim_state.bt
        elif self._sim_state.bt > self._sim_state.min_bt_after_charge + 1.0:
            # Temperature rising by at least 1°C from minimum
            self._sim_state.turning_point_detected = True
            _log.info(
                "Simulator: Turning point detected at %.1f°C",
                self._sim_state.min_bt_after_charge,
            )

    def _check_first_crack(self) -> None:
        """Check if first crack temperature has been reached."""
        if not self._sim_state.roast_started:
            return
        if self._sim_state.fc_triggered:
            return

        if self._sim_state.bt >= self._thermal_config.fc_trigger_temp:
            self._sim_state.fc_triggered = True
            _log.info(
                "Simulator: First crack temperature reached at %.1f°C",
                self._sim_state.bt,
            )

    def reset_thermal_state(self) -> None:
        """Reset the thermal state to initial conditions.

        Useful for starting a new roast simulation.
        """
        config = self._thermal_config
        self._sim_state.bt = config.initial_bt
        self._sim_state.et = config.initial_et
        self._sim_state.at = config.ambient_temp
        self._sim_state.roast_started = False
        self._sim_state.turning_point_detected = False
        self._sim_state.fc_triggered = False
        self._sim_state.min_bt_after_charge = 999.0
        self._sim_state.last_bt = config.initial_bt
        self._sim_state.last_update = time.monotonic()

    # -------------------------------------------------------------------------
    # Network Simulation
    # -------------------------------------------------------------------------

    async def _simulate_network_delay(self) -> None:
        """Simulate network latency if enabled."""
        if not self._network_config.enabled:
            return

        delay_ms = random.randint(
            self._network_config.min_delay_ms,
            self._network_config.max_delay_ms,
        )
        await asyncio.sleep(delay_ms / 1000.0)

    def _should_fail_command(self) -> bool:
        """Check if a command should fail based on failure rate."""
        if not self._network_config.enabled:
            return False
        return random.random() < self._network_config.failure_rate

    def _check_auto_disconnect(self) -> bool:
        """Check if auto-disconnect should trigger."""
        if not self._network_config.enabled:
            return False
        if self._network_config.disconnect_after_seconds <= 0:
            return False

        elapsed = time.monotonic() - self._sim_state.connect_time
        return elapsed >= self._network_config.disconnect_after_seconds

    # -------------------------------------------------------------------------
    # Background Simulation
    # -------------------------------------------------------------------------

    async def _simulation_loop(self) -> None:
        """Background loop to continuously update thermal model."""
        while self._running:
            self._update_thermal_model()

            # Check for auto-disconnect
            if self._check_auto_disconnect():
                _log.warning("Simulator: Auto-disconnect triggered")
                self._is_connected = False
                self.connection_generation += 1
                self._connection_event("closed")
                if self._disconnected_callback:
                    self._disconnected_callback()
                break

            await asyncio.sleep(0.1)  # Update 10 times per second

    # -------------------------------------------------------------------------
    # RoasterDriver Protocol Implementation
    # -------------------------------------------------------------------------

    def add_connection_listener(self, cb: Callable[[dict], None]) -> None:
        if cb not in self._connection_listeners:
            self._connection_listeners.append(cb)

    def remove_connection_listener(self, cb: Callable[[dict], None]) -> None:
        if cb in self._connection_listeners:
            self._connection_listeners.remove(cb)

    def _connection_event(self, kind: str) -> None:
        event = {"ts": datetime.now(UTC).isoformat(), "mono": time.monotonic(),
                 "type": kind, "reason": None, "close_code": None}
        for cb in tuple(self._connection_listeners):
            cb(event.copy())

    async def connect(self, host: str, port: int = 0) -> None:  # noqa: ARG002
        """Connect to the simulator.

        Args:
            host: Ignored (for protocol compatibility)
            port: Ignored (for protocol compatibility)
        """
        if self._is_connected:
            raise ConnectionError("Already connected")

        _log.debug("Simulator: connect() called with host=%s, port=%d (ignored)", host, port)

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated connection failure")

        self._is_connected = True
        self._running = True
        self._sim_state.connect_time = time.monotonic()
        self._sim_state.last_update = time.monotonic()

        # Respond to safety handshake
        self._sim_state.sc = "AR"

        # Start background simulation
        self._simulation_task = asyncio.create_task(self._simulation_loop())

        self._connection_event("connected")
        _log.info("Simulator: Connected (SN: %s)", self._sim_state.sn)

        if self._connected_callback:
            self._connected_callback()

    async def disconnect(self) -> None:
        """Disconnect from the simulator."""
        if not self._is_connected:
            return

        await self._simulate_network_delay()

        self._running = False
        self._is_connected = False
        self.connection_generation += 1
        self._connection_event("closed")

        # Respond to clear guard
        self._sim_state.cl = "AR"

        # Stop background simulation
        if self._simulation_task:
            self._simulation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._simulation_task
            self._simulation_task = None

        _log.info("Simulator: Disconnected")

        if self._disconnected_callback:
            self._disconnected_callback()

    async def set_heat(self, power: int) -> None:
        """Set heating power (0-100%).

        Args:
            power: Heating power percentage (0-100)

        Raises:
            ValueError: If power is not in range 0-100
            ConnectionError: If not connected or command fails
        """
        if not 0 <= power <= 100:
            raise ValueError(f"Power must be 0-100, got {power}")

        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        self._sim_state.hp = power
        _log.debug("Simulator: HP set to %d%%", power)

    async def set_fan(self, speed: int) -> None:
        """Set fan speed (0-100%).

        Args:
            speed: Fan speed percentage (0-100)

        Raises:
            ValueError: If speed is not in range 0-100
            ConnectionError: If not connected or command fails
        """
        if not 0 <= speed <= 100:
            raise ValueError(f"Speed must be 0-100, got {speed}")

        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        self._sim_state.fc = speed
        _log.debug("Simulator: FC set to %d%%", speed)

    async def set_drum(self, speed: int) -> None:
        """Set drum rotation speed (0-100%).

        Args:
            speed: Drum speed percentage (0-100)

        Raises:
            ValueError: If speed is not in range 0-100
            ConnectionError: If not connected or command fails
        """
        if not 0 <= speed <= 100:
            raise ValueError(f"Speed must be 0-100, got {speed}")

        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        self._sim_state.rc = speed
        _log.debug("Simulator: RC set to %d%%", speed)

    async def set_target_temp(self, temp: float) -> None:
        """Set the PID target temperature.

        Args:
            temp: Target temperature in the configured unit
        """
        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        if not 100 <= temp <= 260:
            raise ValueError("Target temperature must be 100-260°C")
        self._sim_state.ts = temp
        _log.debug("Simulator: TS set to %.1f", temp)

    async def set_pid_mode(self, enabled: bool) -> None:
        """Enable or disable PID auto-heating mode.

        Note: In the simulator, PID mode is tracked but doesn't
        affect temperature calculation (manual heat setting is used).

        Args:
            enabled: True to enable PID, False for manual control
        """
        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        self._sim_state.ah = 1 if enabled else 0
        _log.debug("Simulator: AH set to %d", self._sim_state.ah)

    async def set_heating_state(self, enabled: bool) -> None:
        """Enable or disable the heating element.

        This controls the HS (Heating State) variable which must be enabled
        for the heater to actually produce heat in real hardware. In the
        simulator, this is tracked but heat is primarily controlled by HP.

        Args:
            enabled: True to enable heating, False to disable
        """
        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        self._sim_state.hs = 1 if enabled else 0
        _log.debug("Simulator: HS set to %d", self._sim_state.hs)

    async def trigger_event(self, event: RoastEvent) -> None:
        """Trigger a roast event marker.

        Args:
            event: The roast event to trigger
        """
        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated command failure")

        code = self.drop_event_code if event == RoastEvent.DROP else int(event)
        self._sim_state.ev = code

        # Handle special events
        if event == RoastEvent.CHARGE:
            self._sim_state.roast_started = True
            self._sim_state.min_bt_after_charge = self._sim_state.bt
            _log.info("Simulator: CHARGE at %.1f°C", self._sim_state.bt)
        elif event == RoastEvent.TURNING_POINT:
            self._sim_state.turning_point_detected = True
            _log.info("Simulator: TURNING_POINT marked at %.1f°C", self._sim_state.bt)
        elif event == RoastEvent.FC_START:
            self._sim_state.fc_triggered = True
            _log.info("Simulator: FC_START marked at %.1f°C", self._sim_state.bt)
        elif event == RoastEvent.DROP:
            _log.info("Simulator: DROP at %.1f°C", self._sim_state.bt)

        _log.debug("Simulator: EV set to %d (%s)", event.value, event.name)

    async def emergency_stop(self) -> bool:
        """Execute emergency stop.

        Sets heat to 0% and fan to 100% for rapid cooling.
        This bypasses both network delay and failure simulation for safety.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected")

        # Emergency stop always succeeds (safety critical)
        self._sim_state.hp = 0
        self._sim_state.hs = 0
        self._sim_state.fc = 100
        _log.warning("Simulator: Emergency stop - HP=0, FC=100")
        return True

    async def read_state(self) -> KaleidoState:
        """Request and return current state.

        Returns:
            Current state snapshot after thermal model update
        """
        if not self._is_connected:
            raise ConnectionError("Not connected")

        await self._simulate_network_delay()

        if self._should_fail_command():
            raise ConnectionError("Simulated read failure")

        self._update_thermal_model()
        return self._build_state()

    # -------------------------------------------------------------------------
    # Simulator-Specific Methods
    # -------------------------------------------------------------------------

    def get_thermal_config(self) -> ThermalModelConfig:
        """Get the current thermal model configuration."""
        return self._thermal_config

    def set_thermal_config(self, config: ThermalModelConfig) -> None:
        """Update the thermal model configuration.

        Args:
            config: New thermal configuration
        """
        self._thermal_config = config

    def get_network_config(self) -> NetworkSimConfig:
        """Get the current network simulation configuration."""
        return self._network_config

    def set_network_config(self, config: NetworkSimConfig) -> None:
        """Update the network simulation configuration.

        Args:
            config: New network configuration
        """
        self._network_config = config

    def inject_temperature(self, bt: float | None = None, et: float | None = None) -> None:
        """Directly set temperatures for testing.

        Args:
            bt: Bean temperature to set (optional)
            et: Environment temperature to set (optional)
        """
        if bt is not None:
            self._sim_state.bt = bt
        if et is not None:
            self._sim_state.et = et

    def is_turning_point_detected(self) -> bool:
        """Check if turning point has been auto-detected."""
        return self._sim_state.turning_point_detected

    def is_first_crack_triggered(self) -> bool:
        """Check if first crack temperature has been reached."""
        return self._sim_state.fc_triggered
