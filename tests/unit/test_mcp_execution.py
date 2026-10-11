"""Unit tests for execution MCP tools.

Tests start_roast, get_execution_status, and switch_control_mode tools.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.db.models import Coffee, Profile
from maillard.db.schemas import PhaseSchema
from maillard.driver.base import KaleidoState
from maillard.mcp import lifespan
from maillard.mcp.tools.execution import (
    get_execution_status,
    start_roast,
    switch_control_mode,
)
from maillard.roasting.execution import ControlMode, RoastExecutionEngine
from maillard.roasting.session import RoastSession


def _make_mock_context(state: Any) -> Any:
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


def _create_mock_driver(bt: float = 190.0, et: float = 220.0) -> MagicMock:
    """Create a mock driver."""
    driver = MagicMock()
    driver.is_connected = True
    mock_state: KaleidoState = {"BT": bt, "ET": et, "HP": 80, "FC": 40, "RC": 60, "sid": 1}
    driver.state = mock_state
    driver.read_state = AsyncMock(return_value=mock_state)
    driver.set_heat = AsyncMock()
    driver.set_fan = AsyncMock()
    driver.set_drum = AsyncMock()
    driver.set_target_temp = AsyncMock()
    driver.set_pid_mode = AsyncMock()
    driver.trigger_event = AsyncMock()
    driver.emergency_stop = AsyncMock()
    driver.connect = AsyncMock()
    driver.disconnect = AsyncMock()
    return driver


def _make_profile_phases() -> list[dict]:
    """Create a sample profile phases list."""
    phases = [
        PhaseSchema(
            name="charge",
            target_time_seconds=0,
            target_temp=190.0,
            heat_percent=80,
            fan_percent=40,
            drum_percent=60,
        ),
        PhaseSchema(
            name="turning_point",
            target_time_seconds=60,
            target_temp=145.0,
            heat_percent=70,
            fan_percent=45,
            drum_percent=60,
        ),
        PhaseSchema(
            name="drop",
            target_time_seconds=600,
            target_temp=205.0,
            heat_percent=0,
            fan_percent=100,
            drum_percent=60,
        ),
    ]
    return [p.model_dump() for p in phases]


class TestStartRoast:
    """Tests for start_roast tool."""

    @pytest.mark.asyncio
    async def test_requires_connection(self, mongo_db) -> None:
        """Should raise error when roaster not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            # No driver

            with pytest.raises(ConnectionError, match="not connected"):
                await start_roast(ctx, confirmed=True)

    @pytest.mark.asyncio
    async def test_requires_confirmation(self, mongo_db) -> None:
        """Should return confirmation request when not confirmed."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            result = await start_roast(ctx, coffee_id="coffee-1")

            assert result["requires_confirmation"] is True
            assert result["action"] == "start_roast"
            assert "confirmed=True" in result["message"]

    @pytest.mark.asyncio
    async def test_start_without_profile(self, mongo_db) -> None:
        """Should start roast in MANUAL mode without profile."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=195.0)

            # Don't use coffee_id that doesn't exist
            result = await start_roast(ctx, confirmed=True)

            assert result["status"] == "started"
            assert result["execution_mode"] == "manual"
            assert result["charge_temp"] == 195.0
            assert result["roast_id"] is not None
            assert state.roast_session is not None
            assert state._monitor is not None
            assert state._safety_monitor is not None
            # No execution engine in MANUAL mode
            assert state._execution_engine is None

    @pytest.mark.asyncio
    async def test_start_with_profile(self, mongo_db) -> None:
        """Should start roast in PROFILE mode with profile."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=195.0)

            # Create a profile in the database
            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
                charge_temp=190.0,
            )
            await profile.insert()
            profile_id = profile.id

            result = await start_roast(
                ctx,
                profile_id=profile_id,
                confirmed=True,
            )

            assert result["status"] == "started"
            assert result["execution_mode"] == "profile"
            assert result["profile_id"] == profile_id
            assert result["phase_count"] == 3
            assert state._execution_engine is not None
            assert state._execution_engine.mode == ControlMode.PROFILE

    @pytest.mark.asyncio
    async def test_rejects_if_roast_active(self, mongo_db) -> None:
        """Should reject start when roast already active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            # Start first roast (no coffee_id to avoid FK constraint)
            result1 = await start_roast(ctx, confirmed=True)
            assert result1["status"] == "started"

            # Try to start second roast
            result2 = await start_roast(ctx, confirmed=True)
            assert result2["status"] == "error"
            assert "already active" in result2["error"]

    @pytest.mark.asyncio
    async def test_invalid_profile_id(self, mongo_db) -> None:
        """Should error with invalid profile ID."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            result = await start_roast(
                ctx,
                profile_id="nonexistent-profile",
                confirmed=True,
            )

            assert result["status"] == "error"
            assert "not found" in result["error"]


class TestGetExecutionStatus:
    """Tests for get_execution_status tool."""

    @pytest.mark.asyncio
    async def test_no_execution_engine(self, mongo_db) -> None:
        """Should return null engine when none active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await get_execution_status(ctx)

            assert result["execution_engine"] is None
            assert "No execution engine" in result["message"]

    @pytest.mark.asyncio
    async def test_returns_engine_status(self, mongo_db) -> None:
        """Should return execution engine status."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=180.0, et=200.0)

            # Create profile and start roast
            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
                charge_temp=190.0,
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await get_execution_status(ctx)

            assert result["roast_active"] is True
            assert result["execution_engine"] is not None
            assert result["execution_engine"]["mode"] == "profile"
            assert result["execution_engine"]["running"] is True
            assert "current_phase" in result["execution_engine"]
            assert "targets" in result["execution_engine"]

    @pytest.mark.asyncio
    async def test_includes_current_state(self, mongo_db) -> None:
        """Should include current roaster state."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver(bt=175.0, et=195.0)

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await get_execution_status(ctx)

            assert "current_state" in result
            assert result["current_state"]["bt"] == 175.0
            assert result["current_state"]["et"] == 195.0

    @pytest.mark.asyncio
    async def test_includes_session_info(self, mongo_db) -> None:
        """Should include session information."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await get_execution_status(ctx)

            assert "session" in result
            assert result["session"]["state"] == "roasting"
            assert "elapsed_seconds" in result["session"]
            assert "sample_count" in result["session"]


class TestSwitchControlMode:
    """Tests for switch_control_mode tool."""

    @pytest.mark.asyncio
    async def test_no_engine_error(self, mongo_db) -> None:
        """Should error when no execution engine active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await switch_control_mode(ctx, "manual")

            assert result["status"] == "error"
            assert "No execution engine" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_mode_error(self, mongo_db) -> None:
        """Should error with invalid mode."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await switch_control_mode(ctx, "invalid_mode")

            assert result["status"] == "error"
            assert "Invalid mode" in result["error"]

    @pytest.mark.asyncio
    async def test_switch_to_manual(self, mongo_db) -> None:
        """Should switch from PROFILE to MANUAL."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await switch_control_mode(ctx, "manual")

            assert result["status"] == "switched"
            assert result["previous_mode"] == "profile"
            assert result["new_mode"] == "manual"
            assert state._execution_engine.mode == ControlMode.MANUAL

    @pytest.mark.asyncio
    async def test_switch_to_hold(self, mongo_db) -> None:
        """Should switch to HOLD mode."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await switch_control_mode(ctx, "hold")

            assert result["status"] == "switched"
            assert result["new_mode"] == "hold"
            assert state._execution_engine.mode == ControlMode.HOLD

    @pytest.mark.asyncio
    async def test_switch_back_to_profile(self, mongo_db) -> None:
        """Should switch back to PROFILE mode."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            # Switch to manual first
            await switch_control_mode(ctx, "manual")
            assert state._execution_engine.mode == ControlMode.MANUAL

            # Switch back to profile
            result = await switch_control_mode(ctx, "profile")

            assert result["status"] == "switched"
            assert result["previous_mode"] == "manual"
            assert result["new_mode"] == "profile"
            assert state._execution_engine.mode == ControlMode.PROFILE

    @pytest.mark.asyncio
    async def test_case_insensitive_mode(self, mongo_db) -> None:
        """Mode parameter should be case-insensitive."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)

            result = await switch_control_mode(ctx, "MANUAL")

            assert result["status"] == "switched"
            assert result["new_mode"] == "manual"


class TestManualOverrideIntegration:
    """Tests for manual override switching via control tools."""

    @pytest.mark.asyncio
    async def test_set_heat_triggers_manual_mode(self, mongo_db) -> None:
        """Calling set_heat during PROFILE mode should switch to MANUAL."""
        from maillard.mcp.tools.roaster import set_heat

        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)
            assert state._execution_engine.mode == ControlMode.PROFILE

            # Call set_heat
            await set_heat(ctx, 50)

            # Should have switched to MANUAL
            assert state._execution_engine.mode == ControlMode.MANUAL

    @pytest.mark.asyncio
    async def test_set_fan_triggers_manual_mode(self, mongo_db) -> None:
        """Calling set_fan during PROFILE mode should switch to MANUAL."""
        from maillard.mcp.tools.roaster import set_fan

        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)
            assert state._execution_engine.mode == ControlMode.PROFILE

            # Call set_fan
            await set_fan(ctx, 60)

            # Should have switched to MANUAL
            assert state._execution_engine.mode == ControlMode.MANUAL

    @pytest.mark.asyncio
    async def test_set_drum_triggers_manual_mode(self, mongo_db) -> None:
        """Calling set_drum during PROFILE mode should switch to MANUAL."""
        from maillard.mcp.tools.roaster import set_drum

        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            profile = Profile(
                name="Test Profile",
                phases=_make_profile_phases(),
            )
            await profile.insert()
            profile_id = profile.id

            await start_roast(ctx, profile_id=profile_id, confirmed=True)
            assert state._execution_engine.mode == ControlMode.PROFILE

            # Call set_drum
            await set_drum(ctx, 70)

            # Should have switched to MANUAL
            assert state._execution_engine.mode == ControlMode.MANUAL
