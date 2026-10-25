"""Unit tests for SimulatorDriver.

Tests cover:
- Thermal model behavior (heating, cooling, temperature changes)
- Turning point detection
- First crack trigger
- Network delay and failure injection
- RoasterDriver protocol compliance
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from maillard.driver.base import RoastEvent
from maillard.driver.simulator import (
    NetworkSimConfig,
    SimulatorDriver,
    ThermalModelConfig,
)


class TestSimulatorConnection:
    """Tests for connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        """Test successful connection."""
        driver = SimulatorDriver()
        assert not driver.is_connected

        await driver.connect("localhost", 80)
        assert driver.is_connected

        await driver.disconnect()
        assert not driver.is_connected

    @pytest.mark.asyncio
    async def test_connect_already_connected_raises(self) -> None:
        """Test that connecting when already connected raises."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        with pytest.raises(ConnectionError, match="Already connected"):
            await driver.connect("localhost", 80)

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self) -> None:
        """Test that disconnect when not connected is a no-op."""
        driver = SimulatorDriver()
        await driver.disconnect()  # Should not raise
        assert not driver.is_connected

    @pytest.mark.asyncio
    async def test_connected_callback(self) -> None:
        """Test that connected callback is called."""
        callback = Mock()
        driver = SimulatorDriver(connected_callback=callback)

        await driver.connect("localhost", 80)
        callback.assert_called_once()

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_disconnected_callback(self) -> None:
        """Test that disconnected callback is called."""
        callback = Mock()
        driver = SimulatorDriver(disconnected_callback=callback)

        await driver.connect("localhost", 80)
        await driver.disconnect()
        callback.assert_called_once()


class TestSimulatorState:
    """Tests for state management."""

    @pytest.mark.asyncio
    async def test_initial_state(self) -> None:
        """Test initial state values."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        state = await driver.read_state()
        assert state["sid"] == 1
        assert state["TU"] == "C"
        assert state["SN"] == "SIM-001"
        assert state["HP"] == 0
        assert state["FC"] == 0
        assert state["RC"] == 50  # Default drum speed

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_state_property(self) -> None:
        """Test state property returns snapshot."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        state = driver.state
        assert "BT" in state
        assert "ET" in state
        assert "HP" in state

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_custom_initial_temps(self) -> None:
        """Test custom initial temperatures."""
        config = ThermalModelConfig(
            initial_bt=30.0,
            initial_et=35.0,
            ambient_temp=25.0,
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        state = await driver.read_state()
        assert state["BT"] == pytest.approx(30.0, abs=1.0)
        assert state["AT"] == pytest.approx(25.0, abs=0.1)

        await driver.disconnect()


class TestThermalModel:
    """Tests for thermal model behavior."""

    @pytest.mark.asyncio
    async def test_heating_increases_bt(self) -> None:
        """Test that heating power increases BT over time."""
        config = ThermalModelConfig(
            initial_bt=100.0,
            max_heat_rate=15.0,  # 15°C/min
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        initial_state = await driver.read_state()
        initial_bt = initial_state["BT"]

        await driver.set_heating_state(True)
        await driver.set_heat(100)
        await asyncio.sleep(0.5)  # Wait for temperature to change

        final_state = await driver.read_state()
        final_bt = final_state["BT"]

        assert final_bt > initial_bt, "BT should increase with 100% heat"

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_heat_switch_off_prevents_heating(self) -> None:
        """Heat power must not warm the beans while HS is disabled."""
        config = ThermalModelConfig(
            initial_bt=22.0,
            ambient_temp=22.0,
            max_heat_rate=60.0,
            thermal_mass=1.0,
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.set_heating_state(False)
        await driver.set_heat(100)
        await asyncio.sleep(0.2)

        state = await driver.read_state()
        assert state["HS"] == 0
        assert state["HP"] == 100
        assert state["BT"] == pytest.approx(22.0)

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_fan_cooling_effect(self) -> None:
        """Test that fan reduces temperature when hot."""
        config = ThermalModelConfig(
            initial_bt=200.0,  # Start hot
            ambient_temp=22.0,
            max_cool_rate=10.0,  # Higher cooling rate for faster test
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        # Capture initial BT before any updates
        initial_bt = 200.0

        await driver.set_heat(0)
        await driver.set_fan(100)
        await asyncio.sleep(0.5)

        final_state = await driver.read_state()
        final_bt = final_state["BT"]

        # With high cooling rate and 0.5s wait, should cool noticeably
        # Use <= to handle rounding (199.9 should pass when initial is 200.0)
        assert final_bt <= initial_bt - 0.05, (
            f"BT should decrease with 100% fan and no heat, got {final_bt}"
        )

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_heat_rate_proportional_to_power(self) -> None:
        """Test that heat rate is proportional to power percentage."""
        config = ThermalModelConfig(
            initial_bt=100.0,
            max_heat_rate=60.0,  # 60°C/min for easier testing
            thermal_mass=1.0,
        )

        # Test with 50% power
        driver_50 = SimulatorDriver(thermal_config=config)
        await driver_50.connect("localhost", 80)
        await driver_50.set_heating_state(True)
        await driver_50.set_heat(50)
        await asyncio.sleep(0.2)
        state_50 = await driver_50.read_state()
        delta_50 = state_50["BT"] - 100.0
        await driver_50.disconnect()

        # Test with 100% power
        driver_100 = SimulatorDriver(thermal_config=config)
        await driver_100.connect("localhost", 80)
        await driver_100.set_heating_state(True)
        await driver_100.set_heat(100)
        await asyncio.sleep(0.2)
        state_100 = await driver_100.read_state()
        delta_100 = state_100["BT"] - 100.0
        await driver_100.disconnect()

        # 100% power should heat roughly twice as fast as 50%
        assert delta_100 > delta_50 * 1.5, "100% should heat faster than 50%"

    @pytest.mark.asyncio
    async def test_et_tracks_above_bt(self) -> None:
        """Test that ET stays above BT during heating."""
        # Start with realistic preheat temps where ET > BT
        config = ThermalModelConfig(
            initial_bt=100.0,
            initial_et=120.0,  # ET typically higher than BT
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.set_heating_state(True)
        await driver.set_heat(80)
        await asyncio.sleep(0.5)

        state = await driver.read_state()
        # ET should remain above BT during active heating
        assert state["ET"] > state["BT"], (
            f"ET ({state['ET']}) should be above BT ({state['BT']}) during roasting"
        )

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_temperature_clamped_to_range(self) -> None:
        """Test that temperatures are clamped to valid ranges."""
        config = ThermalModelConfig(initial_bt=279.0)
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.set_heating_state(True)
        await driver.set_heat(100)
        await asyncio.sleep(0.5)

        state = await driver.read_state()
        assert state["BT"] <= 280.0, "BT should be clamped to max 280°C"

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_reset_thermal_state(self) -> None:
        """Test resetting thermal state to initial values."""
        config = ThermalModelConfig(initial_bt=22.0)
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        # Heat up
        await driver.set_heating_state(True)
        await driver.set_heat(100)
        await asyncio.sleep(0.3)
        state = await driver.read_state()
        assert state["BT"] > 22.0

        # Reset
        driver.reset_thermal_state()
        state = await driver.read_state()
        assert state["BT"] == pytest.approx(22.0, abs=1.0)

        await driver.disconnect()


class TestTurningPointDetection:
    """Tests for turning point auto-detection."""

    @pytest.mark.asyncio
    async def test_turning_point_not_detected_before_charge(self) -> None:
        """Test that TP is not detected before CHARGE event."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        assert not driver.is_turning_point_detected()

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_turning_point_detected_after_charge(self) -> None:
        """Test that TP is detected when temperature rises after charge."""
        config = ThermalModelConfig(
            initial_bt=150.0,  # Start at preheat temp
            max_heat_rate=30.0,
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        # Simulate charge (temperature will drop briefly due to cold beans)
        driver.inject_temperature(bt=90.0)  # Simulate bean charge drop
        await driver.trigger_event(RoastEvent.CHARGE)

        assert not driver.is_turning_point_detected()

        # Now increase heat and wait for TP detection
        await driver.set_heat(100)
        # Inject rising temperatures to trigger TP
        driver.inject_temperature(bt=91.5)
        await driver.read_state()  # Trigger update

        assert driver.is_turning_point_detected()

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_turning_point_detection_can_be_disabled(self) -> None:
        """Test that TP detection can be disabled."""
        config = ThermalModelConfig(tp_detection_enabled=False)
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.trigger_event(RoastEvent.CHARGE)
        driver.inject_temperature(bt=92.0)
        await driver.read_state()

        assert not driver.is_turning_point_detected()

        await driver.disconnect()


class TestFirstCrackTrigger:
    """Tests for first crack temperature trigger."""

    @pytest.mark.asyncio
    async def test_fc_not_triggered_below_threshold(self) -> None:
        """Test that FC is not triggered below threshold."""
        config = ThermalModelConfig(
            initial_bt=180.0,
            fc_trigger_temp=196.0,
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.trigger_event(RoastEvent.CHARGE)
        await driver.read_state()

        assert not driver.is_first_crack_triggered()

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_fc_triggered_at_threshold(self) -> None:
        """Test that FC is triggered when reaching threshold temp."""
        config = ThermalModelConfig(
            initial_bt=180.0,
            fc_trigger_temp=196.0,
        )
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.trigger_event(RoastEvent.CHARGE)

        # Inject temperature at FC threshold
        driver.inject_temperature(bt=196.5)
        await driver.read_state()

        assert driver.is_first_crack_triggered()

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_configurable_fc_trigger_temp(self) -> None:
        """Test that FC trigger temperature is configurable."""
        config = ThermalModelConfig(fc_trigger_temp=200.0)
        driver = SimulatorDriver(thermal_config=config)
        await driver.connect("localhost", 80)

        await driver.trigger_event(RoastEvent.CHARGE)

        # Below custom threshold
        driver.inject_temperature(bt=198.0)
        await driver.read_state()
        assert not driver.is_first_crack_triggered()

        # At custom threshold
        driver.inject_temperature(bt=200.5)
        await driver.read_state()
        assert driver.is_first_crack_triggered()

        await driver.disconnect()


class TestNetworkSimulation:
    """Tests for network delay and failure injection."""

    @pytest.mark.asyncio
    async def test_network_delay_applied(self) -> None:
        """Test that network delay is applied to commands."""
        config = NetworkSimConfig(
            enabled=True,
            min_delay_ms=50,
            max_delay_ms=50,  # Fixed delay for testing
        )
        driver = SimulatorDriver(network_config=config)

        start = asyncio.get_event_loop().time()
        await driver.connect("localhost", 80)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed >= 0.05, "Connection should have delay"

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_command_failure_injection(self) -> None:
        """Test that commands can be made to fail."""
        config = NetworkSimConfig(
            enabled=True,
            failure_rate=1.0,  # 100% failure
        )
        driver = SimulatorDriver(network_config=config)

        with pytest.raises(ConnectionError, match="Simulated connection failure"):
            await driver.connect("localhost", 80)

    @pytest.mark.asyncio
    async def test_partial_failure_rate(self) -> None:
        """Test that partial failure rate works."""
        config = NetworkSimConfig(
            enabled=True,
            failure_rate=0.5,  # 50% failure
            min_delay_ms=0,
            max_delay_ms=0,
        )
        driver = SimulatorDriver(network_config=config)

        # Try multiple times - some should succeed
        successes = 0
        failures = 0
        for _ in range(20):
            try:
                await driver.connect("localhost", 80)
                successes += 1
                await driver.disconnect()
            except ConnectionError:
                failures += 1

        # With 50% rate over 20 tries, we should have both
        assert successes > 0, "Some connections should succeed"
        assert failures > 0, "Some connections should fail"

    @pytest.mark.asyncio
    async def test_emergency_stop_bypasses_failure(self) -> None:
        """Test that emergency_stop ignores failure injection."""
        config = NetworkSimConfig(
            enabled=True,
            failure_rate=0.0,  # No failure for connect
        )
        driver = SimulatorDriver(network_config=config)
        await driver.connect("localhost", 80)

        # Set failure rate high for commands
        driver.set_network_config(
            NetworkSimConfig(enabled=True, failure_rate=1.0, min_delay_ms=0, max_delay_ms=0)
        )

        # Emergency stop should still work (safety critical)
        await driver.emergency_stop()  # Should not raise

        # Verify emergency stop worked by checking internal state directly
        # (read_state would fail due to 100% failure rate)
        assert driver._sim_state.hp == 0, "Emergency stop should set HP to 0"
        assert driver._sim_state.fc == 100, "Emergency stop should set FC to 100"

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_auto_disconnect_after_timeout(self) -> None:
        """Test that auto-disconnect triggers after configured time."""
        callback = Mock()
        config = NetworkSimConfig(
            enabled=True,
            disconnect_after_seconds=0.2,  # Short timeout for test
            min_delay_ms=0,
            max_delay_ms=0,
        )
        driver = SimulatorDriver(network_config=config, disconnected_callback=callback)
        await driver.connect("localhost", 80)

        assert driver.is_connected

        # Wait for auto-disconnect to trigger
        await asyncio.sleep(0.3)

        assert not driver.is_connected, "Should have auto-disconnected"
        callback.assert_called_once()

        # Cleanup (disconnect is no-op if already disconnected)
        await driver.disconnect()


class TestRoasterControlCommands:
    """Tests for roaster control commands."""

    @pytest.mark.asyncio
    async def test_set_heat_updates_state(self) -> None:
        """Test that set_heat updates HP state."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.set_heat(75)
        state = await driver.read_state()
        assert state["HP"] == 75

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_heat_validates_range(self) -> None:
        """Test that set_heat validates 0-100 range."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        with pytest.raises(ValueError, match="Power must be 0-100"):
            await driver.set_heat(101)

        with pytest.raises(ValueError, match="Power must be 0-100"):
            await driver.set_heat(-1)

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_fan_updates_state(self) -> None:
        """Test that set_fan updates FC state."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.set_fan(60)
        state = await driver.read_state()
        assert state["FC"] == 60

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_fan_validates_range(self) -> None:
        """Test that set_fan validates 0-100 range."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        with pytest.raises(ValueError, match="Speed must be 0-100"):
            await driver.set_fan(150)

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_drum_updates_state(self) -> None:
        """Test that set_drum updates RC state."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.set_drum(80)
        state = await driver.read_state()
        assert state["RC"] == 80

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_drum_validates_range(self) -> None:
        """Test that set_drum validates 0-100 range."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        with pytest.raises(ValueError, match="Speed must be 0-100"):
            await driver.set_drum(-5)

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_target_temp(self) -> None:
        """Test setting PID target temperature."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.set_target_temp(180.5)
        state = await driver.read_state()
        assert state["TS"] == pytest.approx(180.5, abs=0.1)

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_set_pid_mode(self) -> None:
        """Test enabling/disabling PID mode."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.set_pid_mode(True)
        state = await driver.read_state()
        assert state["AH"] == 1

        await driver.set_pid_mode(False)
        state = await driver.read_state()
        assert state["AH"] == 0

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_emergency_stop(self) -> None:
        """Test emergency stop sets HP=0 and FC=100."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.set_heat(80)
        await driver.set_fan(30)
        await driver.emergency_stop()

        state = await driver.read_state()
        assert state["HP"] == 0
        assert state["FC"] == 100

        await driver.disconnect()


class TestRoastEvents:
    """Tests for roast event handling."""

    @pytest.mark.asyncio
    async def test_trigger_charge_event(self) -> None:
        """Test triggering CHARGE event."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.trigger_event(RoastEvent.CHARGE)
        state = await driver.read_state()
        assert state["EV"] == RoastEvent.CHARGE.value

        await driver.disconnect()

    @pytest.mark.asyncio
    async def test_trigger_drop_event(self) -> None:
        """DROP sends EV 8 by default."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        await driver.trigger_event(RoastEvent.DROP)
        state = await driver.read_state()
        assert state["EV"] == 8

        await driver.disconnect()

        legacy = SimulatorDriver(drop_event_code=9)
        await legacy.connect("localhost", 80)
        await legacy.trigger_event(RoastEvent.DROP)
        assert (await legacy.read_state())["EV"] == 9
        await legacy.disconnect()

    @pytest.mark.asyncio
    async def test_all_event_types(self) -> None:
        """Test all roast event types can be triggered."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        events = [
            RoastEvent.CHARGE,
            RoastEvent.TURNING_POINT,
            RoastEvent.DRY_END,
            RoastEvent.FC_START,
            RoastEvent.FC_END,
            RoastEvent.SC_START,
            RoastEvent.SC_END,
            RoastEvent.DROP,
        ]

        for event in events:
            await driver.trigger_event(event)
            state = await driver.read_state()
            assert state["EV"] == event.value

        await driver.disconnect()


class TestConnectionNotConnectedErrors:
    """Tests for errors when not connected."""

    @pytest.mark.asyncio
    async def test_set_heat_requires_connection(self) -> None:
        """Test that set_heat raises when not connected."""
        driver = SimulatorDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            await driver.set_heat(50)

    @pytest.mark.asyncio
    async def test_set_fan_requires_connection(self) -> None:
        """Test that set_fan raises when not connected."""
        driver = SimulatorDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            await driver.set_fan(50)

    @pytest.mark.asyncio
    async def test_set_drum_requires_connection(self) -> None:
        """Test that set_drum raises when not connected."""
        driver = SimulatorDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            await driver.set_drum(50)

    @pytest.mark.asyncio
    async def test_trigger_event_requires_connection(self) -> None:
        """Test that trigger_event raises when not connected."""
        driver = SimulatorDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            await driver.trigger_event(RoastEvent.CHARGE)

    @pytest.mark.asyncio
    async def test_read_state_requires_connection(self) -> None:
        """Test that read_state raises when not connected."""
        driver = SimulatorDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            await driver.read_state()


class TestConfigurationMethods:
    """Tests for configuration getter/setter methods."""

    def test_get_thermal_config(self) -> None:
        """Test getting thermal configuration."""
        config = ThermalModelConfig(max_heat_rate=20.0)
        driver = SimulatorDriver(thermal_config=config)

        retrieved = driver.get_thermal_config()
        assert retrieved.max_heat_rate == 20.0

    def test_set_thermal_config(self) -> None:
        """Test setting thermal configuration."""
        driver = SimulatorDriver()
        new_config = ThermalModelConfig(max_heat_rate=25.0)
        driver.set_thermal_config(new_config)

        retrieved = driver.get_thermal_config()
        assert retrieved.max_heat_rate == 25.0

    def test_get_network_config(self) -> None:
        """Test getting network configuration."""
        config = NetworkSimConfig(enabled=True, min_delay_ms=100)
        driver = SimulatorDriver(network_config=config)

        retrieved = driver.get_network_config()
        assert retrieved.enabled is True
        assert retrieved.min_delay_ms == 100

    def test_set_network_config(self) -> None:
        """Test setting network configuration."""
        driver = SimulatorDriver()
        new_config = NetworkSimConfig(enabled=True, failure_rate=0.5)
        driver.set_network_config(new_config)

        retrieved = driver.get_network_config()
        assert retrieved.failure_rate == 0.5

    @pytest.mark.asyncio
    async def test_inject_temperature(self) -> None:
        """Test injecting temperatures directly."""
        driver = SimulatorDriver()
        await driver.connect("localhost", 80)

        driver.inject_temperature(bt=150.0, et=170.0)
        state = await driver.read_state()

        assert state["BT"] == pytest.approx(150.0, abs=1.0)
        # ET might drift slightly due to thermal model update

        await driver.disconnect()
