"""Unit tests for MCP roaster control tools.

Tests all roaster control functionality including:
- Connection management (connect, disconnect, status)
- Event markers (charge, turning point, dry end, first crack, drop)
- Control settings (heat, fan, drum, target temp, PID)
- Emergency stop
- Human-in-the-loop confirmation flow
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from maillard.core.events import EventBus
from maillard.driver.base import KaleidoState, RoastEvent
from maillard.mcp import lifespan
from maillard.mcp.state import ApplicationState
from maillard.mcp.tools.roaster import (
    connect_roaster,
    disconnect_roaster,
    emergency_stop,
    execute_charge,
    execute_drop,
    get_roaster_status,
    mark_dry_end,
    mark_first_crack,
    mark_first_crack_end,
    mark_second_crack,
    mark_turning_point,
    set_drum,
    set_fan,
    set_heat,
    set_target_temp,
    start_preheat,
    toggle_pid,
)
from maillard.roasting.session import RoastSession


# Note: reset_global_state fixture from conftest.py is applied automatically


def _setup_active_roast(
    state: ApplicationState,
    *,
    roast_id: str = "roast-123",
    coffee_id: str | None = None,
    profile_id: str | None = None,
    charge_temp: float = 195.0,
) -> RoastSession:
    """Set up an active roast session on the given state.

    Creates a RoastSession and configures roast_context for backward compat.
    """
    session = RoastSession.start(
        event_bus=state.event_bus,
        charge_temp=charge_temp,
        roast_id=roast_id,
        coffee_id=coffee_id,
        profile_id=profile_id,
    )
    state.roast_session = session
    state.roast_context.is_active = True
    state.roast_context.roast_id = roast_id
    state.roast_context.coffee_id = coffee_id
    state.roast_context.profile_id = profile_id
    state.roast_context.charge_time = session.machine.charge_time.timestamp() if session.machine.charge_time else None
    return session


def _make_mock_context(state: Any) -> Any:
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


def _create_mock_driver(
    is_connected: bool = True,
    bt: float = 100.0,
    et: float = 120.0,
    hp: int = 50,
    fc: int = 40,
    rc: int = 60,
    ts: float = 200.0,
    ah: int = 0,
) -> MagicMock:
    """Create a mock driver with specified state."""
    driver = MagicMock()
    driver.is_connected = is_connected

    # Create a real KaleidoState for the state property
    mock_state: KaleidoState = {
        "BT": bt,
        "ET": et,
        "HP": hp,
        "FC": fc,
        "RC": rc,
        "TS": ts,
        "AH": ah,
        "sid": 1,
    }
    driver.state = mock_state

    # Make async methods return awaitable
    driver.set_heat = AsyncMock()
    driver.set_fan = AsyncMock()
    driver.set_drum = AsyncMock()
    driver.set_heating_state = AsyncMock()
    driver.set_target_temp = AsyncMock()
    driver.set_pid_mode = AsyncMock()
    driver.trigger_event = AsyncMock()
    driver.emergency_stop = AsyncMock(return_value=True)
    driver.read_state = AsyncMock(return_value=mock_state)
    driver.connect = AsyncMock()
    driver.disconnect = AsyncMock()
    driver._send_message = AsyncMock()

    return driver


# =============================================================================
# get_roaster_status Tests
# =============================================================================


class TestGetRoasterStatus:
    """Tests for get_roaster_status tool."""

    @pytest.mark.asyncio
    async def test_status_not_connected(self, mongo_db) -> None:
        """Should return not connected status when no driver."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            result = await get_roaster_status(ctx)

            assert result["roaster"]["connected"] is False
            assert result["roaster"]["bt"] is None
            assert result["roast"]["active"] is False

    @pytest.mark.asyncio
    async def test_status_connected(self, mongo_db) -> None:
        """Should return roaster state when connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=150.0, et=170.0, hp=70, fc=50)

            result = await get_roaster_status(ctx)

            assert result["roaster"]["connected"] is True
            assert result["roaster"]["bt"] == 150.0
            assert result["roaster"]["et"] == 170.0
            assert result["roaster"]["heat_percent"] == 70
            assert result["roaster"]["fan_percent"] == 50

    @pytest.mark.asyncio
    async def test_status_with_active_roast(self, mongo_db) -> None:
        """Should include roast info when roast is active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state, coffee_id="coffee-123", profile_id="profile-456")

            result = await get_roaster_status(ctx)

            assert result["roast"]["active"] is True
            assert result["roast"]["coffee_id"] == "coffee-123"
            assert result["roast"]["profile_id"] == "profile-456"
            assert result["roast"]["state"] == "roasting"
            assert result["roast"]["phase"] is not None


# =============================================================================
# connect_roaster Tests
# =============================================================================


class TestConnectRoaster:
    """Tests for connect_roaster tool."""

    @pytest.mark.asyncio
    async def test_connect_already_connected(self, mongo_db) -> None:
        """Should return already_connected when already connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(is_connected=True)

            result = await connect_roaster(ctx)

            assert result["status"] == "already_connected"

    @pytest.mark.asyncio
    async def test_connect_with_simulator(self, mongo_db) -> None:
        """Should connect using simulator driver."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            # Mock the connect_roaster method on state
            with patch.object(state, "connect_roaster", new_callable=AsyncMock) as mock_connect:
                # After connect, driver should be set
                mock_driver = _create_mock_driver()

                async def side_effect(**kwargs: Any) -> None:
                    state.driver = mock_driver

                mock_connect.side_effect = side_effect

                result = await connect_roaster(ctx, driver_type="simulator")

                assert result["status"] == "connected"
                mock_connect.assert_called_once_with(
                    driver_type="simulator",
                    host=state.config.kaleido_host,
                    port=state.config.kaleido_port,
                )

    @pytest.mark.asyncio
    async def test_connect_invalid_driver_type(self, mongo_db) -> None:
        """Should raise error for invalid driver type."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ValueError, match="Invalid driver_type"):
                await connect_roaster(ctx, driver_type="invalid")


# =============================================================================
# disconnect_roaster Tests
# =============================================================================


class TestDisconnectRoaster:
    """Tests for disconnect_roaster tool."""

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self, mongo_db) -> None:
        """Should return not_connected when no driver."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await disconnect_roaster(ctx)

            assert result["status"] == "not_connected"

    @pytest.mark.asyncio
    async def test_disconnect_while_roast_active(self, mongo_db) -> None:
        """Should refuse to disconnect while roast is active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state)

            result = await disconnect_roaster(ctx)

            assert result["status"] == "roast_active"
            assert "roast_id" in result

    @pytest.mark.asyncio
    async def test_disconnect_success(self, mongo_db) -> None:
        """Should disconnect successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            with patch.object(state, "disconnect_roaster", new_callable=AsyncMock) as mock_disconnect:
                result = await disconnect_roaster(ctx)

                assert result["status"] == "disconnected"
                mock_disconnect.assert_called_once()


# =============================================================================
# execute_charge Tests
# =============================================================================


class TestExecuteCharge:
    """Tests for execute_charge tool."""

    @pytest.mark.asyncio
    async def test_charge_not_connected(self, mongo_db) -> None:
        """Should raise error when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ConnectionError, match="not connected"):
                await execute_charge(ctx)

    @pytest.mark.asyncio
    async def test_charge_roast_already_active(self, mongo_db) -> None:
        """Should return error when roast already active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state, roast_id="existing-roast")

            result = await execute_charge(ctx, confirmed=True)
            assert result["status"] == "error"
            assert "already active" in result["error"]

    @pytest.mark.asyncio
    async def test_charge_requires_confirmation(self, mongo_db) -> None:
        """Should return confirmation request when not confirmed."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=200.0)

            result = await execute_charge(ctx, confirmed=False)

            assert result["requires_confirmation"] is True
            assert result["action"] == "execute_charge"
            assert result["current_bt"] == 200.0

    @pytest.mark.asyncio
    async def test_charge_confirmed(self, mongo_db) -> None:
        """Should execute charge when confirmed, creating DB record and session."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            result = await execute_charge(
                ctx,
                confirmed=True,
            )

            assert result["status"] == "charged"
            assert result["charge_temp"] == 195.0
            assert result["roast_id"] is not None
            driver.trigger_event.assert_called_once_with(RoastEvent.CHARGE)
            assert state.roast_context.is_active is True
            # Session should be created
            assert state.roast_session is not None
            assert state.roast_session.is_active is True


# =============================================================================
# Event Marker Tests
# =============================================================================


class TestMarkTurningPoint:
    """Tests for mark_turning_point tool."""

    @pytest.mark.asyncio
    async def test_turning_point_not_connected(self, mongo_db) -> None:
        """Should raise error when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ConnectionError):
                await mark_turning_point(ctx)

    @pytest.mark.asyncio
    async def test_turning_point_no_active_roast(self, mongo_db) -> None:
        """Should return error dict when no active roast."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            result = await mark_turning_point(ctx)
            assert result["status"] == "error"
            assert "No active roast" in result["error"]

    @pytest.mark.asyncio
    async def test_turning_point_success(self, mongo_db) -> None:
        """Should mark turning point successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=95.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_turning_point(ctx)

            assert result["status"] == "marked"
            assert result["event"] == "turning_point"
            assert result["temp"] == 95.0
            driver.trigger_event.assert_called_once_with(RoastEvent.TURNING_POINT)


class TestMarkDryEnd:
    """Tests for mark_dry_end tool."""

    @pytest.mark.asyncio
    async def test_dry_end_success(self, mongo_db) -> None:
        """Should mark dry end successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=150.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_dry_end(ctx)

            assert result["status"] == "marked"
            assert result["event"] == "dry_end"
            assert result["temp"] == 150.0
            driver.trigger_event.assert_called_once_with(RoastEvent.DRY_END)


class TestMarkFirstCrack:
    """Tests for mark_first_crack tool."""

    @pytest.mark.asyncio
    async def test_first_crack_requires_confirmation(self, mongo_db) -> None:
        """Should require confirmation for first crack."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=196.0)
            _setup_active_roast(state)

            result = await mark_first_crack(ctx, confirmed=False)

            assert result["requires_confirmation"] is True
            assert result["action"] == "mark_first_crack"

    @pytest.mark.asyncio
    async def test_first_crack_confirmed(self, mongo_db) -> None:
        """Should mark first crack when confirmed."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=196.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_first_crack(ctx, confirmed=True)

            assert result["status"] == "marked"
            assert result["event"] == "fc_start"
            driver.trigger_event.assert_called_once_with(RoastEvent.FC_START)


class TestMarkFirstCrackEnd:
    """Tests for mark_first_crack_end tool."""

    @pytest.mark.asyncio
    async def test_first_crack_end_success(self, mongo_db) -> None:
        """Should mark first crack end successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=202.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_first_crack_end(ctx)

            assert result["status"] == "marked"
            assert result["event"] == "fc_end"
            driver.trigger_event.assert_called_once_with(RoastEvent.FC_END)


class TestMarkSecondCrack:
    """Tests for mark_second_crack tool."""

    @pytest.mark.asyncio
    async def test_second_crack_success(self, mongo_db) -> None:
        """Should mark second crack successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=224.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_second_crack(ctx)

            assert result["status"] == "marked"
            assert result["event"] == "sc_start"
            driver.trigger_event.assert_called_once_with(RoastEvent.SC_START)


# =============================================================================
# execute_drop Tests
# =============================================================================


class TestExecuteDrop:
    """Tests for execute_drop tool."""

    @pytest.mark.asyncio
    async def test_drop_not_connected(self, mongo_db) -> None:
        """Should raise error when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ConnectionError):
                await execute_drop(ctx)

    @pytest.mark.asyncio
    async def test_drop_no_active_roast(self, mongo_db) -> None:
        """Should return error when no active roast."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            result = await execute_drop(ctx, confirmed=True)
            assert result["status"] == "error"
            assert "No active roast" in result["error"]

    @pytest.mark.asyncio
    async def test_drop_requires_confirmation(self, mongo_db) -> None:
        """Should require confirmation for drop."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=210.0)
            _setup_active_roast(state)

            result = await execute_drop(ctx, confirmed=False)

            assert result["requires_confirmation"] is True
            assert result["action"] == "execute_drop"

    @pytest.mark.asyncio
    async def test_drop_confirmed(self, mongo_db) -> None:
        """Should execute drop when confirmed, persisting to DB."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver
            _setup_active_roast(state, coffee_id="coffee-123")

            result = await execute_drop(ctx, confirmed=True)

            assert result["status"] == "dropped"
            assert result["drop_temp"] == 210.0
            assert result["coffee_id"] == "coffee-123"
            assert result["roast_id"] is not None
            driver.trigger_event.assert_called_once_with(RoastEvent.DROP)
            # Session and context should be cleared
            assert state.roast_session is None
            assert state.roast_context.is_active is False


# =============================================================================
# Control Tool Tests
# =============================================================================


class TestSetHeat:
    """Tests for set_heat tool."""

    @pytest.mark.asyncio
    async def test_set_heat_not_connected(self, mongo_db) -> None:
        """Should raise error when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ConnectionError):
                await set_heat(ctx, 50)

    @pytest.mark.asyncio
    async def test_set_heat_invalid_range(self, mongo_db) -> None:
        """Should raise error for invalid heat percentage."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            with pytest.raises(ValueError, match="0-100"):
                await set_heat(ctx, 150)

            with pytest.raises(ValueError, match="0-100"):
                await set_heat(ctx, -10)

    @pytest.mark.asyncio
    async def test_set_heat_success(self, mongo_db) -> None:
        """Should set heat successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await set_heat(ctx, 75)

            assert result["status"] == "set"
            assert result["percent"] == 75
            driver.set_heat.assert_called_once_with(75)


class TestSetFan:
    """Tests for set_fan tool."""

    @pytest.mark.asyncio
    async def test_set_fan_success(self, mongo_db) -> None:
        """Should set fan successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await set_fan(ctx, 60)

            assert result["status"] == "set"
            assert result["percent"] == 60
            driver.set_fan.assert_called_once_with(60)

    @pytest.mark.asyncio
    async def test_set_fan_invalid_range(self, mongo_db) -> None:
        """Should raise error for invalid fan percentage."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            with pytest.raises(ValueError, match="0-100"):
                await set_fan(ctx, 101)


class TestSetDrum:
    """Tests for set_drum tool."""

    @pytest.mark.asyncio
    async def test_set_drum_success(self, mongo_db) -> None:
        """Should set drum successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await set_drum(ctx, 55)

            assert result["status"] == "set"
            assert result["percent"] == 55
            driver.set_drum.assert_called_once_with(55)


class TestSetTargetTemp:
    """Tests for set_target_temp tool."""

    @pytest.mark.asyncio
    async def test_set_target_temp_success(self, mongo_db) -> None:
        """Should set target temp successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await set_target_temp(ctx, 200.5)

            assert result["status"] == "set"
            assert result["temp_celsius"] == 200.5
            driver.set_target_temp.assert_called_once_with(200.5)

    @pytest.mark.asyncio
    async def test_set_target_temp_invalid_range(self, mongo_db) -> None:
        """Should raise error for invalid temperature."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            with pytest.raises(ValueError, match="0-300"):
                await set_target_temp(ctx, 350.0)


class TestTogglePid:
    """Tests for toggle_pid tool."""

    @pytest.mark.asyncio
    async def test_toggle_pid_enable(self, mongo_db) -> None:
        """Should enable PID mode."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await toggle_pid(ctx, True)

            assert result["status"] == "set"
            assert result["enabled"] is True
            driver.set_pid_mode.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_toggle_pid_disable(self, mongo_db) -> None:
        """Should disable PID mode."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await toggle_pid(ctx, False)

            assert result["status"] == "set"
            assert result["enabled"] is False
            driver.set_pid_mode.assert_called_once_with(False)


# =============================================================================
# emergency_stop Tests
# =============================================================================


class TestEmergencyStop:
    """Tests for emergency_stop tool."""

    @pytest.mark.asyncio
    async def test_emergency_stop_not_connected(self, mongo_db) -> None:
        """Should raise error when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ConnectionError):
                await emergency_stop(ctx)

    @pytest.mark.asyncio
    async def test_emergency_stop_success(self, mongo_db) -> None:
        """Should execute emergency stop successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            result = await emergency_stop(ctx)

            assert result["status"] == "emergency_stopped"
            assert result["heat_percent"] == 0
            assert result["fan_percent"] == 100
            driver.emergency_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_emergency_stop_aborts_active_roast(self, mongo_db) -> None:
        """Should abort active roast on emergency stop."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver
            _setup_active_roast(state)

            result = await emergency_stop(ctx)

            assert result["roast_aborted"] is True
            assert result["aborted_roast_id"] == "roast-123"
            # Session and context should be cleared
            assert state.roast_session is None
            assert state.roast_context.is_active is False


# =============================================================================
# start_preheat Tests
# =============================================================================


class TestStartPreheat:
    """Tests for start_preheat tool."""

    @pytest.mark.asyncio
    async def test_preheat_not_connected(self, mongo_db) -> None:
        """Should raise error when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            with pytest.raises(ConnectionError):
                await start_preheat(ctx, 200.0)

    @pytest.mark.asyncio
    async def test_preheat_invalid_temp(self, mongo_db) -> None:
        """Should raise error for invalid preheat temperature."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            with pytest.raises(ValueError, match="100-250"):
                await start_preheat(ctx, 50.0)

            with pytest.raises(ValueError, match="100-250"):
                await start_preheat(ctx, 300.0)

    @pytest.mark.asyncio
    async def test_preheat_while_roast_active(self, mongo_db) -> None:
        """Should return error when roast is active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state)

            result = await start_preheat(ctx, 200.0)
            assert result["status"] == "error"
            assert "roast is active" in result["error"]

    @pytest.mark.asyncio
    async def test_preheat_success(self, mongo_db) -> None:
        """Should start preheating successfully."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=50.0)
            state.driver = driver

            result = await start_preheat(ctx, 200.0)

            assert result["status"] == "preheating"
            assert result["target_temp"] == 200.0
            assert result["current_bt"] == 50.0
            assert result["heating_enabled"] is True
            driver.set_target_temp.assert_called_once_with(200.0)


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_control_at_boundary_values(self, mongo_db) -> None:
        """Should accept boundary values for controls."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver()
            state.driver = driver

            # Test 0%
            result = await set_heat(ctx, 0)
            assert result["percent"] == 0

            # Test 100%
            result = await set_fan(ctx, 100)
            assert result["percent"] == 100

    @pytest.mark.asyncio
    async def test_multiple_events_in_sequence(self, mongo_db) -> None:
        """Should handle multiple events in sequence."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=200.0)
            state.driver = driver
            _setup_active_roast(state)

            # Mark multiple events
            result1 = await mark_turning_point(ctx)
            assert result1["status"] == "marked"
            result2 = await mark_dry_end(ctx)
            assert result2["status"] == "marked"
            result3 = await mark_first_crack(ctx, confirmed=True)
            assert result3["status"] == "marked"
            result4 = await execute_drop(ctx, confirmed=True)
            assert result4["status"] == "dropped"

            # Verify all events were triggered on hardware
            assert driver.trigger_event.call_count == 4


class TestConfirmationFlow:
    """Tests for the confirmation flow pattern."""

    @pytest.mark.asyncio
    async def test_two_call_pattern_charge(self, mongo_db) -> None:
        """Should follow two-call pattern: first returns confirmation, second executes."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            # First call - get confirmation request
            result1 = await execute_charge(ctx)
            assert result1["requires_confirmation"] is True
            assert "current_bt" in result1
            # Driver should NOT have been called
            driver.trigger_event.assert_not_called()

            # Second call - execute with confirmation
            result2 = await execute_charge(ctx, confirmed=True)
            assert result2["status"] == "charged"
            # Driver should now have been called
            driver.trigger_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_call_pattern_drop(self, mongo_db) -> None:
        """Should follow two-call pattern for drop."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver
            _setup_active_roast(state)

            # First call - get confirmation request
            result1 = await execute_drop(ctx)
            assert result1["requires_confirmation"] is True

            # Second call - execute with confirmation
            result2 = await execute_drop(ctx, confirmed=True)
            assert result2["status"] == "dropped"


# =============================================================================
# Invalid State Transition Tests (AC #9)
# =============================================================================


class TestInvalidStateTransitions:
    """Tests that invalid state transitions return error dicts, not exceptions."""

    @pytest.mark.asyncio
    async def test_marker_without_active_roast(self, mongo_db) -> None:
        """All markers should return error dict when no roast is active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            for tool in [mark_turning_point, mark_dry_end, mark_first_crack_end, mark_second_crack]:
                result = await tool(ctx)
                assert result["status"] == "error", f"{tool.__name__} did not return error"
                assert "No active roast" in result["error"]

            # first_crack needs confirmed=True to bypass confirmation
            result = await mark_first_crack(ctx, confirmed=True)
            assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_drop_without_active_roast(self, mongo_db) -> None:
        """Drop should return error dict when no roast is active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            result = await execute_drop(ctx, confirmed=True)
            assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_preheat_creates_state_machine(self, mongo_db) -> None:
        """Preheat should create a state machine for later charge."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=50.0)
            state.driver = driver

            result = await start_preheat(ctx, 200.0)

            assert result["status"] == "preheating"
            assert state.roast_session is not None
            assert state.roast_session.machine.state_enum.value == "preheating"


# =============================================================================
# Monitor Lifecycle Tests
# =============================================================================


class TestMonitorLifecycleInTools:
    """Tests that monitor starts on charge and stops on drop/emergency_stop."""

    @pytest.mark.asyncio
    async def test_charge_starts_monitor(self, mongo_db) -> None:
        """execute_charge should create and start a RoastMonitor."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            result = await execute_charge(ctx, confirmed=True)

            assert result["status"] == "charged"
            assert state._monitor is not None
            assert state._monitor.is_running is True

            # Clean up
            await state._monitor.stop()

    @pytest.mark.asyncio
    async def test_drop_stops_monitor(self, mongo_db) -> None:
        """execute_drop should stop the monitor."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver

            # Charge first (creates monitor)
            await execute_charge(ctx, confirmed=True)
            assert state._monitor is not None

            # Drop should stop monitor
            result = await execute_drop(ctx, confirmed=True)
            assert result["status"] == "dropped"
            assert state._monitor is None

    @pytest.mark.asyncio
    async def test_emergency_stop_stops_monitor(self, mongo_db) -> None:
        """emergency_stop should stop the monitor."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=200.0)
            state.driver = driver

            # Charge first (creates monitor)
            await execute_charge(ctx, confirmed=True)
            assert state._monitor is not None

            result = await emergency_stop(ctx)
            assert result["status"] == "emergency_stopped"
            assert state._monitor is None


__all__ = [
    "TestConfirmationFlow",
    "TestConnectRoaster",
    "TestDisconnectRoaster",
    "TestEdgeCases",
    "TestEmergencyStop",
    "TestExecuteCharge",
    "TestExecuteDrop",
    "TestGetRoasterStatus",
    "TestMarkDryEnd",
    "TestMarkFirstCrack",
    "TestMarkFirstCrackEnd",
    "TestMarkSecondCrack",
    "TestMarkTurningPoint",
    "TestSetDrum",
    "TestSetFan",
    "TestSetHeat",
    "TestSetTargetTemp",
    "TestStartPreheat",
    "TestTogglePid",
]
