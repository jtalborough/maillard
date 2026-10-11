"""Unit tests for get_roast_progress MCP tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.core.events import EventBus
from maillard.driver.base import KaleidoState
from maillard.mcp import lifespan
from maillard.mcp.state import ApplicationState
from maillard.mcp.tools.monitoring import get_roast_progress
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
    driver.set_heating_state = AsyncMock()
    driver.set_target_temp = AsyncMock()
    driver.set_pid_mode = AsyncMock()
    driver.trigger_event = AsyncMock()
    driver.emergency_stop = AsyncMock(return_value=True)
    driver.connect = AsyncMock()
    driver.disconnect = AsyncMock()
    return driver


class TestGetRoastProgress:
    """Tests for get_roast_progress tool."""

    @pytest.mark.asyncio
    async def test_no_active_roast(self, mongo_db) -> None:
        """Should return no_active_roast when no session exists."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)

            result = await get_roast_progress(ctx)

            assert result["status"] == "no_active_roast"

    @pytest.mark.asyncio
    async def test_active_roast_returns_data(self, mongo_db) -> None:
        """Should return roast progress data when roast is active."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            driver = _create_mock_driver(bt=195.0, et=225.0)
            state.driver = driver

            # Start a roast session
            session = RoastSession.start(
                event_bus=state.event_bus,
                charge_temp=195.0,
                roast_id="roast-test",
            )
            state.roast_session = session

            result = await get_roast_progress(ctx)

            assert result["status"] == "active"
            assert result["bt"] == 195.0
            assert result["et"] == 225.0
            assert result["sample_count"] >= 0
            assert result["current_phase"] is not None
            assert isinstance(result["events"], list)
            assert result["elapsed_seconds"] is not None

    @pytest.mark.asyncio
    async def test_events_included(self, mongo_db) -> None:
        """Should include marked events in the response."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            session = RoastSession.start(
                event_bus=state.event_bus,
                charge_temp=195.0,
                roast_id="roast-test",
            )
            state.roast_session = session

            result = await get_roast_progress(ctx)

            # At minimum, charge event should be present
            assert len(result["events"]) >= 1
            charge_event = result["events"][0]
            assert charge_event["type"] == "charge"

    @pytest.mark.asyncio
    async def test_monitor_running_field(self, mongo_db) -> None:
        """Should report monitor_running status."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            state.driver = _create_mock_driver()

            session = RoastSession.start(
                event_bus=state.event_bus,
                charge_temp=195.0,
                roast_id="roast-test",
            )
            state.roast_session = session

            result = await get_roast_progress(ctx)

            # No monitor set on state
            assert result["monitor_running"] is False

    @pytest.mark.asyncio
    async def test_no_driver_connected(self, mongo_db) -> None:
        """Should return None temps when driver not connected."""
        async with lifespan() as state:
            ctx = _make_mock_context(state)
            # No driver

            session = RoastSession.start(
                event_bus=state.event_bus,
                charge_temp=195.0,
                roast_id="roast-test",
            )
            state.roast_session = session

            result = await get_roast_progress(ctx)

            assert result["status"] == "active"
            assert result["bt"] is None
            assert result["et"] is None
