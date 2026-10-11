"""Unit tests for human-in-the-loop confirmation flow.

Tests the two-call pattern for critical roasting actions:
- execute_charge: Starting the roast
- mark_first_crack: Confirming audible crack was heard
- execute_drop: Ending the roast

The confirmation pattern ensures the AI cannot execute these critical,
irreversible actions without explicit human confirmation.

Flow:
1. First call with confirmed=False returns {requires_confirmation: True, ...}
2. Human reviews the information and decides to proceed
3. Second call with confirmed=True executes the action
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.driver.base import KaleidoState, RoastEvent
from maillard.mcp import lifespan
from maillard.mcp.state import ApplicationState
from maillard.mcp.tools.roaster import (
    execute_charge,
    execute_drop,
    mark_first_crack,
)
from maillard.roasting.session import RoastSession


def _setup_active_roast(
    state: ApplicationState,
    *,
    roast_id: str = "roast-123",
    coffee_id: str | None = None,
    profile_id: str | None = None,
) -> RoastSession:
    """Set up an active roast session on the given state."""
    session = RoastSession.start(
        event_bus=state.event_bus,
        charge_temp=195.0,
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
    bt: float = 200.0,
    et: float = 220.0,
) -> MagicMock:
    """Create a mock driver with specified state."""
    driver = MagicMock()
    driver.is_connected = is_connected

    mock_state: KaleidoState = {
        "BT": bt,
        "ET": et,
        "HP": 50,
        "FC": 40,
        "RC": 60,
        "TS": 200.0,
        "AH": 0,
        "sid": 1,
    }
    driver.state = mock_state

    driver.trigger_event = AsyncMock()
    driver.set_heat = AsyncMock()
    driver.set_fan = AsyncMock()
    driver.set_heating_state = AsyncMock()
    driver.emergency_stop = AsyncMock()

    return driver


# =============================================================================
# Confirmation Response Structure Tests
# =============================================================================


class TestConfirmationResponseStructure:
    """Tests for the structure of confirmation response messages."""

    @pytest.mark.asyncio
    async def test_charge_confirmation_response_fields(self, mongo_db) -> None:
        """Charge confirmation response should have all required fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=195.0)

            result = await execute_charge(ctx, confirmed=False)

            # Required fields for confirmation response
            assert "requires_confirmation" in result
            assert result["requires_confirmation"] is True
            assert "action" in result
            assert result["action"] == "execute_charge"
            assert "message" in result
            assert isinstance(result["message"], str)
            assert "current_bt" in result
            assert result["current_bt"] == 195.0

    @pytest.mark.asyncio
    async def test_first_crack_confirmation_response_fields(self, mongo_db) -> None:
        """First crack confirmation response should have all required fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=196.0)
            _setup_active_roast(state)

            result = await mark_first_crack(ctx, confirmed=False)

            assert "requires_confirmation" in result
            assert result["requires_confirmation"] is True
            assert "action" in result
            assert result["action"] == "mark_first_crack"
            assert "message" in result
            assert "current_bt" in result
            assert "elapsed_seconds" in result

    @pytest.mark.asyncio
    async def test_drop_confirmation_response_fields(self, mongo_db) -> None:
        """Drop confirmation response should have all required fields."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=210.0)
            _setup_active_roast(state)

            result = await execute_drop(ctx, confirmed=False)

            assert "requires_confirmation" in result
            assert result["requires_confirmation"] is True
            assert "action" in result
            assert result["action"] == "execute_drop"
            assert "message" in result
            assert "current_bt" in result
            assert "elapsed_seconds" in result


# =============================================================================
# Two-Call Pattern Tests
# =============================================================================


class TestTwoCallPatternCharge:
    """Tests for execute_charge two-call confirmation pattern."""

    @pytest.mark.asyncio
    async def test_charge_first_call_does_not_execute(self, mongo_db) -> None:
        """First call with confirmed=False should NOT trigger the roaster."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            await execute_charge(ctx, confirmed=False)

            # Driver should NOT have been called
            driver.trigger_event.assert_not_called()
            # Roast should NOT be active
            assert state.roast_context.is_active is False

    @pytest.mark.asyncio
    async def test_charge_second_call_executes(self, mongo_db) -> None:
        """Second call with confirmed=True should execute the charge."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            result = await execute_charge(ctx, confirmed=True)

            # Driver should have been called
            driver.trigger_event.assert_called_once_with(RoastEvent.CHARGE)
            # Result should indicate success
            assert result["status"] == "charged"
            # Roast should now be active
            assert state.roast_context.is_active is True

    @pytest.mark.asyncio
    async def test_charge_complete_two_call_flow(self, mongo_db) -> None:
        """Complete flow: unconfirmed → confirmation request → confirmed → executed."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            # First call - get confirmation request (IDs don't need to exist for unconfirmed)
            result1 = await execute_charge(
                ctx, coffee_id="coffee-123", profile_id="profile-456", confirmed=False
            )
            assert result1["requires_confirmation"] is True
            assert result1["coffee_id"] == "coffee-123"
            assert result1["profile_id"] == "profile-456"
            driver.trigger_event.assert_not_called()

            # Second call - execute with confirmation (no FK IDs to avoid constraint)
            result2 = await execute_charge(ctx, confirmed=True)
            assert result2["status"] == "charged"
            driver.trigger_event.assert_called_once()


class TestTwoCallPatternFirstCrack:
    """Tests for mark_first_crack two-call confirmation pattern."""

    @pytest.mark.asyncio
    async def test_first_crack_first_call_does_not_execute(self, mongo_db) -> None:
        """First call with confirmed=False should NOT trigger the roaster."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=196.0)
            state.driver = driver
            _setup_active_roast(state)

            await mark_first_crack(ctx, confirmed=False)

            # Driver should NOT have been called
            driver.trigger_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_crack_second_call_executes(self, mongo_db) -> None:
        """Second call with confirmed=True should mark first crack."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=196.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_first_crack(ctx, confirmed=True)

            # Driver should have been called
            driver.trigger_event.assert_called_once_with(RoastEvent.FC_START)
            # Result should indicate success
            assert result["status"] == "marked"
            assert result["event"] == "fc_start"

    @pytest.mark.asyncio
    async def test_first_crack_complete_two_call_flow(self, mongo_db) -> None:
        """Complete flow for first crack confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=196.0)
            state.driver = driver
            _setup_active_roast(state)

            # First call - get confirmation request
            result1 = await mark_first_crack(ctx, confirmed=False)
            assert result1["requires_confirmation"] is True
            assert "196" in result1["message"]  # Temperature mentioned
            driver.trigger_event.assert_not_called()

            # Second call - execute with confirmation
            result2 = await mark_first_crack(ctx, confirmed=True)
            assert result2["status"] == "marked"
            driver.trigger_event.assert_called_once()


class TestTwoCallPatternDrop:
    """Tests for execute_drop two-call confirmation pattern."""

    @pytest.mark.asyncio
    async def test_drop_first_call_does_not_execute(self, mongo_db) -> None:
        """First call with confirmed=False should NOT trigger the roaster."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver
            _setup_active_roast(state)

            await execute_drop(ctx, confirmed=False)

            # Driver should NOT have been called
            driver.trigger_event.assert_not_called()
            # Roast should still be active
            assert state.roast_context.is_active is True

    @pytest.mark.asyncio
    async def test_drop_second_call_executes(self, mongo_db) -> None:
        """Second call with confirmed=True should execute the drop."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await execute_drop(ctx, confirmed=True)

            # Driver should have been called
            driver.trigger_event.assert_called_once_with(RoastEvent.DROP)
            # Result should indicate success
            assert result["status"] == "dropped"
            # Roast should no longer be active
            assert state.roast_context.is_active is False

    @pytest.mark.asyncio
    async def test_drop_complete_two_call_flow(self, mongo_db) -> None:
        """Complete flow for drop confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver
            _setup_active_roast(state, coffee_id="coffee-123")

            # First call - get confirmation request
            result1 = await execute_drop(ctx, confirmed=False)
            assert result1["requires_confirmation"] is True
            assert "210" in result1["message"]  # Temperature mentioned
            driver.trigger_event.assert_not_called()

            # Second call - execute with confirmation
            result2 = await execute_drop(ctx, confirmed=True)
            assert result2["status"] == "dropped"
            assert result2["coffee_id"] == "coffee-123"
            driver.trigger_event.assert_called_once()


# =============================================================================
# Direct Confirmation Tests (Skipping First Call)
# =============================================================================


class TestDirectConfirmation:
    """Tests for direct confirmation (skipping the first unconfirmed call).

    Users may choose to call with confirmed=True directly if they are
    certain they want to proceed.
    """

    @pytest.mark.asyncio
    async def test_charge_direct_confirmed(self, mongo_db) -> None:
        """Should be able to charge directly with confirmed=True."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0)
            state.driver = driver

            result = await execute_charge(ctx, confirmed=True)

            assert result["status"] == "charged"
            driver.trigger_event.assert_called_once_with(RoastEvent.CHARGE)

    @pytest.mark.asyncio
    async def test_first_crack_direct_confirmed(self, mongo_db) -> None:
        """Should be able to mark first crack directly with confirmed=True."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=196.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await mark_first_crack(ctx, confirmed=True)

            assert result["status"] == "marked"
            driver.trigger_event.assert_called_once_with(RoastEvent.FC_START)

    @pytest.mark.asyncio
    async def test_drop_direct_confirmed(self, mongo_db) -> None:
        """Should be able to drop directly with confirmed=True."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=210.0)
            state.driver = driver
            _setup_active_roast(state)

            result = await execute_drop(ctx, confirmed=True)

            assert result["status"] == "dropped"
            driver.trigger_event.assert_called_once_with(RoastEvent.DROP)


# =============================================================================
# Error Handling with Confirmation
# =============================================================================


class TestConfirmationErrorHandling:
    """Tests for error handling in the confirmation flow."""

    @pytest.mark.asyncio
    async def test_charge_unconfirmed_when_not_connected(self, mongo_db) -> None:
        """Should raise error when calling charge without connection."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            # No driver connected

            with pytest.raises(ConnectionError, match="not connected"):
                await execute_charge(ctx, confirmed=False)

    @pytest.mark.asyncio
    async def test_charge_confirmed_when_not_connected(self, mongo_db) -> None:
        """Should raise error even with confirmed=True when not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            # No driver connected

            with pytest.raises(ConnectionError, match="not connected"):
                await execute_charge(ctx, confirmed=True)

    @pytest.mark.asyncio
    async def test_first_crack_without_active_roast(self, mongo_db) -> None:
        """Should return error dict when marking first crack without active roast."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            # No active roast

            result = await mark_first_crack(ctx, confirmed=False)
            assert result["status"] == "error"
            assert "No active roast" in result["error"]

    @pytest.mark.asyncio
    async def test_drop_without_active_roast(self, mongo_db) -> None:
        """Should return error dict when dropping without active roast."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            # No active roast

            result = await execute_drop(ctx, confirmed=False)
            assert result["status"] == "error"
            assert "No active roast" in result["error"]

    @pytest.mark.asyncio
    async def test_charge_when_roast_already_active(self, mongo_db) -> None:
        """Should return error when trying to charge with roast already active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state, roast_id="existing-roast")

            result = await execute_charge(ctx, confirmed=False)
            assert result["status"] == "error"
            assert "already active" in result["error"]


# =============================================================================
# Confirmation Message Content Tests
# =============================================================================


class TestConfirmationMessageContent:
    """Tests for the content of confirmation messages."""

    @pytest.mark.asyncio
    async def test_charge_message_includes_temperature(self, mongo_db) -> None:
        """Charge confirmation message should include current BT."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=195.5)

            result = await execute_charge(ctx, confirmed=False)

            assert "195.5" in result["message"]
            assert "CHARGE" in result["message"]

    @pytest.mark.asyncio
    async def test_first_crack_message_includes_temperature_and_time(self, mongo_db) -> None:
        """First crack confirmation message should include BT and elapsed time."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=196.0)
            _setup_active_roast(state)  # Past time

            result = await mark_first_crack(ctx, confirmed=False)

            # Message should mention temperature
            assert "196.0" in result["message"]
            # Message should mention first crack
            assert "FIRST CRACK" in result["message"]
            # elapsed_seconds should be present
            assert result["elapsed_seconds"] is not None

    @pytest.mark.asyncio
    async def test_drop_message_includes_temperature_and_time(self, mongo_db) -> None:
        """Drop confirmation message should include BT and elapsed time."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=210.5)
            _setup_active_roast(state)

            result = await execute_drop(ctx, confirmed=False)

            assert "210.5" in result["message"]
            assert "DROP" in result["message"]
            assert result["elapsed_seconds"] is not None

    @pytest.mark.asyncio
    async def test_charge_message_instructs_how_to_confirm(self, mongo_db) -> None:
        """Charge confirmation message should explain how to confirm."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            result = await execute_charge(ctx, confirmed=False)

            assert "confirmed=True" in result["message"]


# =============================================================================
# State Preservation Tests
# =============================================================================


class TestStatePreservationDuringConfirmation:
    """Tests that state is preserved correctly during the confirmation flow."""

    @pytest.mark.asyncio
    async def test_charge_preserves_coffee_and_profile_ids(self, mongo_db) -> None:
        """Charge should preserve coffee_id and profile_id through confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            # First call includes IDs
            result1 = await execute_charge(
                ctx, coffee_id="coffee-abc", profile_id="profile-xyz", confirmed=False
            )
            assert result1["coffee_id"] == "coffee-abc"
            assert result1["profile_id"] == "profile-xyz"

    @pytest.mark.asyncio
    async def test_drop_preserves_roast_context(self, mongo_db) -> None:
        """Drop should include context from the roast in its response."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=210.0)
            _setup_active_roast(state, coffee_id="coffee-xyz", profile_id="profile-abc")

            result = await execute_drop(ctx, confirmed=True)

            assert result["coffee_id"] == "coffee-xyz"
            assert result["profile_id"] == "profile-abc"


# =============================================================================
# Default Parameter Tests
# =============================================================================


class TestDefaultConfirmationParameter:
    """Tests that confirmed defaults to False for safety."""

    @pytest.mark.asyncio
    async def test_charge_defaults_to_unconfirmed(self, mongo_db) -> None:
        """Charge should default to requiring confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            # Call without confirmed parameter
            result = await execute_charge(ctx)

            assert result["requires_confirmation"] is True

    @pytest.mark.asyncio
    async def test_first_crack_defaults_to_unconfirmed(self, mongo_db) -> None:
        """First crack should default to requiring confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state)

            # Call without confirmed parameter
            result = await mark_first_crack(ctx)

            assert result["requires_confirmation"] is True

    @pytest.mark.asyncio
    async def test_drop_defaults_to_unconfirmed(self, mongo_db) -> None:
        """Drop should default to requiring confirmation."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()
            _setup_active_roast(state)

            # Call without confirmed parameter
            result = await execute_drop(ctx)

            assert result["requires_confirmation"] is True


__all__ = [
    "TestConfirmationErrorHandling",
    "TestConfirmationMessageContent",
    "TestConfirmationResponseStructure",
    "TestDefaultConfirmationParameter",
    "TestDirectConfirmation",
    "TestStatePreservationDuringConfirmation",
    "TestTwoCallPatternCharge",
    "TestTwoCallPatternDrop",
    "TestTwoCallPatternFirstCrack",
]
