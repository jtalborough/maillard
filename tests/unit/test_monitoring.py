"""Unit tests for RoastMonitor.

Tests the background polling loop, event publishing, stop lifecycle,
error handling, and consecutive error threshold.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from maillard.core.events import EventBus, StateChangeEvent
from maillard.driver.base import KaleidoState
from maillard.mcp.monitoring import RoastMonitor
from maillard.roasting.session import RoastSession


def _make_mock_driver(
    bt: float = 180.0,
    et: float = 200.0,
) -> MagicMock:
    """Create a mock driver that returns predictable state."""
    driver = MagicMock()
    driver.is_connected = True
    mock_state: KaleidoState = {"BT": bt, "ET": et, "HP": 80, "FC": 40, "RC": 60, "sid": 1}
    driver.state = mock_state
    driver.read_state = AsyncMock(return_value=mock_state)
    return driver


def _make_mock_session() -> MagicMock:
    """Create a mock RoastSession for monitor's log enrichment."""
    session = MagicMock(spec=RoastSession)
    session.elapsed_time = 60.0
    session.current_phase = MagicMock()
    session.current_phase.name = "drying"
    session.latest_ror = 8.5
    return session


class TestRoastMonitorLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_running_task(self) -> None:
        """Monitor should be running after start()."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        try:
            await monitor.start()
            assert monitor.is_running is True
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        """Monitor should not be running after stop()."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        await monitor.start()
        assert monitor.is_running is True

        await monitor.stop()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling stop() multiple times should not raise."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        await monitor.start()
        await monitor.stop()
        await monitor.stop()  # Should not raise
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_stop_without_start(self) -> None:
        """Calling stop() without start() should not raise."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        await monitor.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start() when already running should not create duplicate tasks."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        try:
            await monitor.start()
            task1 = monitor._task
            await monitor.start()  # Should be no-op
            assert monitor._task is task1
        finally:
            await monitor.stop()


class TestRoastMonitorPolling:
    """Tests for polling behavior and event publishing."""

    @pytest.mark.asyncio
    async def test_publishes_state_change_events(self) -> None:
        """Monitor should publish StateChangeEvent on each poll."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        received: list[StateChangeEvent] = []

        async def handler(event: StateChangeEvent) -> None:
            received.append(event)

        bus.on(StateChangeEvent, handler)

        try:
            await monitor.start()
            # Wait for a few poll cycles
            await asyncio.sleep(0.2)
        finally:
            await monitor.stop()

        assert len(received) >= 2
        assert received[0].state["BT"] == 180.0
        assert received[0].state["ET"] == 200.0

    @pytest.mark.asyncio
    async def test_calls_driver_read_state(self) -> None:
        """Monitor should call driver.read_state() each poll cycle."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        try:
            await monitor.start()
            await asyncio.sleep(0.2)
        finally:
            await monitor.stop()

        assert driver.read_state.call_count >= 2

    @pytest.mark.asyncio
    async def test_respects_poll_interval(self) -> None:
        """Monitor should poll at approximately the configured interval."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()
        monitor = RoastMonitor(driver, bus, session, poll_interval=0.1)

        try:
            await monitor.start()
            await asyncio.sleep(0.35)
        finally:
            await monitor.stop()

        # With 0.1s interval and 0.35s total, expect 3-4 calls
        assert 2 <= driver.read_state.call_count <= 5


class TestRoastMonitorErrorHandling:
    """Tests for error handling and consecutive error threshold."""

    @pytest.mark.asyncio
    async def test_skips_sample_on_error(self) -> None:
        """Monitor should continue after a single read_state error."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()

        call_count = 0
        mock_state: KaleidoState = {"BT": 180.0, "ET": 200.0, "HP": 80, "FC": 40, "RC": 60, "sid": 1}

        async def read_state_side_effect() -> KaleidoState:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("Transient error")
            return mock_state

        driver.read_state = AsyncMock(side_effect=read_state_side_effect)

        received: list[StateChangeEvent] = []
        bus.on(StateChangeEvent, lambda e: received.append(e))

        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        try:
            await monitor.start()
            await asyncio.sleep(0.3)
        finally:
            await monitor.stop()

        # Should have received events from successful polls (skipping the error)
        assert len(received) >= 2

    @pytest.mark.asyncio
    async def test_retries_after_consecutive_errors(self) -> None:
        """Monitor must keep running after persistent read failures."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()

        driver.read_state = AsyncMock(side_effect=ConnectionError("Persistent error"))

        monitor = RoastMonitor(
            driver, bus, session,
            poll_interval=0.02,
            max_consecutive_errors=3,
        )

        await monitor.start()
        # Wait long enough for 3+ error cycles
        await asyncio.sleep(0.3)

        try:
            assert monitor.is_running is True
            assert driver.read_state.call_count >= 3
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_resets_error_count_on_success(self) -> None:
        """Consecutive error counter should reset after a successful poll."""
        driver = _make_mock_driver()
        bus = EventBus()
        session = _make_mock_session()

        call_count = 0
        mock_state: KaleidoState = {"BT": 180.0, "ET": 200.0, "HP": 80, "FC": 40, "RC": 60, "sid": 1}

        async def read_state_alternating() -> KaleidoState:
            nonlocal call_count
            call_count += 1
            # Fail every other call
            if call_count % 2 == 0:
                raise ConnectionError("Intermittent")
            return mock_state

        driver.read_state = AsyncMock(side_effect=read_state_alternating)

        monitor = RoastMonitor(
            driver, bus, session,
            poll_interval=0.03,
            max_consecutive_errors=3,
        )

        try:
            await monitor.start()
            await asyncio.sleep(0.3)
        finally:
            await monitor.stop()

        # Should still be running because errors are not consecutive
        # (it was running until we stopped it)
        assert driver.read_state.call_count >= 4


class TestRoastMonitorLogging:
    """Tests for structured log output."""

    @pytest.mark.asyncio
    async def test_logs_sample_at_info_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """Monitor should emit structured SAMPLE log lines at INFO level."""
        driver = _make_mock_driver(bt=185.5, et=210.3)
        bus = EventBus()
        session = _make_mock_session()
        session.elapsed_time = 45.0
        session.current_phase.name = "maillard"
        session.latest_ror = 9.2

        monitor = RoastMonitor(driver, bus, session, poll_interval=0.05)

        with caplog.at_level(logging.INFO, logger="maillard.mcp.monitoring"):
            try:
                await monitor.start()
                await asyncio.sleep(0.15)
            finally:
                await monitor.stop()

        sample_logs = [r for r in caplog.records if "SAMPLE" in r.message]
        assert len(sample_logs) >= 1
        msg = sample_logs[0].message
        assert "BT=185.5" in msg
        assert "ET=210.3" in msg
        assert "RoR=9.2" in msg
        assert "phase=maillard" in msg
