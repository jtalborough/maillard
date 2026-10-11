"""Unit tests for HeatingManager.

Tests the background HS maintenance loop, start/stop lifecycle,
error handling, and status reporting.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maillard.core.heating import HeatingManager


def _make_mock_driver(
    has_send_message: bool = True,
) -> MagicMock:
    """Create a mock driver with optional _send_message method."""
    driver = MagicMock()
    driver.is_connected = True

    if has_send_message:
        driver._send_message = AsyncMock()
    else:
        # Remove the attribute so hasattr returns False
        if hasattr(driver, "_send_message"):
            delattr(driver, "_send_message")

    driver.set_heating_state = AsyncMock()
    driver.set_heat = AsyncMock()
    return driver


class TestHeatingManagerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_running_task(self) -> None:
        """Manager should be running after start()."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        try:
            await manager.start(heat_percent=80)
            assert manager.is_running is True
            assert manager.heat_percent == 80
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        """Manager should not be running after stop()."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        await manager.start(heat_percent=80)
        assert manager.is_running is True

        await manager.stop()
        assert manager.is_running is False

    @pytest.mark.asyncio
    async def test_stop_sends_hs_zero(self) -> None:
        """Manager should send HS=0 when stopped."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        await manager.start(heat_percent=80)
        await asyncio.sleep(0.1)  # Let a few sends happen
        await manager.stop()

        # Last call should be HS=0
        calls = driver._send_message.call_args_list
        last_hs_call = [c for c in calls if c[0][0] == "HS"][-1]
        assert last_hs_call[0][1] == "0"

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling stop() multiple times should not raise."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        await manager.start(heat_percent=80)
        await manager.stop()
        await manager.stop()  # Should not raise
        assert manager.is_running is False

    @pytest.mark.asyncio
    async def test_stop_without_start(self) -> None:
        """Calling stop() without start() should not raise."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        await manager.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start() when already running should update heat percent."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        try:
            await manager.start(heat_percent=80)
            task1 = manager._task
            await manager.start(heat_percent=60)  # Should update, not create new task
            assert manager._task is task1
            assert manager.heat_percent == 60
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_start_validates_heat_percent(self) -> None:
        """Start should raise ValueError for invalid heat percent."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        with pytest.raises(ValueError, match="heat_percent must be 0-100"):
            await manager.start(heat_percent=150)

        with pytest.raises(ValueError, match="heat_percent must be 0-100"):
            await manager.start(heat_percent=-10)


class TestHeatingManagerSendLoop:
    """Tests for the background HS=1 send loop."""

    @pytest.mark.asyncio
    async def test_sends_hs_and_hp_commands(self) -> None:
        """Manager should send HS=1 and HP commands repeatedly."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        try:
            await manager.start(heat_percent=75)
            await asyncio.sleep(0.2)  # Allow several send cycles
        finally:
            await manager.stop()

        # Check that HS=1 and HP=75 were sent
        hs_calls = [c for c in driver._send_message.call_args_list if c[0][0] == "HS" and c[0][1] == "1"]
        hp_calls = [c for c in driver._send_message.call_args_list if c[0][0] == "HP" and c[0][1] == "75"]

        assert len(hs_calls) >= 2
        assert len(hp_calls) >= 2

    @pytest.mark.asyncio
    async def test_respects_send_interval(self) -> None:
        """Manager should send at approximately the configured interval."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.1)

        try:
            await manager.start(heat_percent=80)
            await asyncio.sleep(0.35)
        finally:
            await manager.stop()

        # With 0.1s interval and 0.35s total, expect 3-4 HS sends
        hs_calls = [c for c in driver._send_message.call_args_list if c[0][0] == "HS"]
        assert 2 <= len(hs_calls) <= 5

    @pytest.mark.asyncio
    async def test_update_heat_changes_hp_value(self) -> None:
        """update_heat should change the HP value sent in subsequent loops."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        try:
            await manager.start(heat_percent=80)
            await asyncio.sleep(0.1)

            await manager.update_heat(40)
            await asyncio.sleep(0.1)
        finally:
            await manager.stop()

        # Should have HP=80 and HP=40 calls
        hp_calls = [c[0][1] for c in driver._send_message.call_args_list if c[0][0] == "HP"]
        assert "80" in hp_calls
        assert "40" in hp_calls

    @pytest.mark.asyncio
    async def test_update_heat_validates_percent(self) -> None:
        """update_heat should raise ValueError for invalid percent."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        with pytest.raises(ValueError, match="heat_percent must be 0-100"):
            await manager.update_heat(101)


class TestHeatingManagerFallback:
    """Tests for fallback behavior when _send_message is not available."""

    @pytest.mark.asyncio
    async def test_uses_driver_methods_as_fallback(self) -> None:
        """Manager should use driver.set_heating_state if _send_message unavailable."""
        driver = _make_mock_driver(has_send_message=False)
        manager = HeatingManager(driver, send_interval=0.05)

        try:
            await manager.start(heat_percent=70)
            await asyncio.sleep(0.15)
        finally:
            await manager.stop()

        # Should use set_heating_state and set_heat instead
        assert driver.set_heating_state.call_count >= 2
        assert driver.set_heat.call_count >= 2
        driver.set_heating_state.assert_any_call(True)


class TestHeatingManagerErrorHandling:
    """Tests for error handling and consecutive error threshold."""

    @pytest.mark.asyncio
    async def test_continues_after_single_error(self) -> None:
        """Manager should continue after a single send error."""
        driver = _make_mock_driver()

        call_count = 0

        async def send_with_error(tag: str, value: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("Transient error")

        driver._send_message = AsyncMock(side_effect=send_with_error)
        manager = HeatingManager(driver, send_interval=0.05, max_consecutive_errors=5)

        try:
            await manager.start(heat_percent=80)
            await asyncio.sleep(0.3)
        finally:
            await manager.stop()

        # Should still be running (error was recovered)
        status = manager.get_status()
        assert status["total_sends"] > 2
        assert status["total_errors"] >= 1

    @pytest.mark.asyncio
    async def test_stops_after_consecutive_errors(self) -> None:
        """Manager should stop after max consecutive errors."""
        driver = _make_mock_driver()
        driver._send_message = AsyncMock(side_effect=ConnectionError("Persistent error"))

        manager = HeatingManager(
            driver,
            send_interval=0.02,
            max_consecutive_errors=3,
        )

        await manager.start(heat_percent=80)
        # Wait long enough for 3+ error cycles (each cycle sends 2 messages)
        await asyncio.sleep(0.5)

        # Manager should have stopped itself
        assert manager.is_running is False

        status = manager.get_status()
        assert status["consecutive_errors"] >= 3

    @pytest.mark.asyncio
    async def test_resets_error_count_on_success(self) -> None:
        """Consecutive error counter should reset after a successful send."""
        driver = _make_mock_driver()

        call_count = 0

        async def send_alternating(tag: str, value: str) -> None:
            nonlocal call_count
            call_count += 1
            # Fail every 4th call (so we get some failures but not consecutive)
            if call_count % 4 == 0:
                raise ConnectionError("Intermittent error")

        driver._send_message = AsyncMock(side_effect=send_alternating)

        manager = HeatingManager(
            driver,
            send_interval=0.03,
            max_consecutive_errors=3,
        )

        try:
            await manager.start(heat_percent=80)
            await asyncio.sleep(0.3)
        finally:
            await manager.stop()

        # Should still be running because errors are not consecutive
        status = manager.get_status()
        assert status["total_errors"] >= 1
        assert status["consecutive_errors"] < 3


class TestHeatingManagerStatus:
    """Tests for get_status() method."""

    @pytest.mark.asyncio
    async def test_status_includes_all_fields(self) -> None:
        """Status should include all expected fields."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.1)

        try:
            await manager.start(heat_percent=85)
            await asyncio.sleep(0.15)
            status = manager.get_status()
        finally:
            await manager.stop()

        assert status["active"] is True
        assert status["heat_percent"] == 85
        assert status["send_interval"] == 0.1
        assert status["last_send_time"] is not None
        assert status["consecutive_errors"] == 0
        assert status["total_sends"] >= 1
        assert status["total_errors"] == 0

    @pytest.mark.asyncio
    async def test_status_when_not_running(self) -> None:
        """Status should show inactive when manager is not running."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver)

        status = manager.get_status()

        assert status["active"] is False
        assert status["heat_percent"] == 0
        assert status["last_send_time"] is None

    @pytest.mark.asyncio
    async def test_status_tracks_total_sends(self) -> None:
        """Status should accurately track total sends."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.03)

        try:
            await manager.start(heat_percent=80)
            await asyncio.sleep(0.15)
            status1 = manager.get_status()
            await asyncio.sleep(0.15)
            status2 = manager.get_status()
        finally:
            await manager.stop()

        assert status2["total_sends"] > status1["total_sends"]


class TestHeatingManagerIntegration:
    """Integration tests with simulated driver behavior."""

    @pytest.mark.asyncio
    async def test_full_preheat_cycle(self) -> None:
        """Test a complete preheat cycle: start, run, stop."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        # Start preheating
        await manager.start(heat_percent=100)
        assert manager.is_running is True
        await asyncio.sleep(0.1)

        # Reduce heat
        await manager.update_heat(80)
        await asyncio.sleep(0.1)

        # Get status mid-preheat
        status = manager.get_status()
        assert status["active"] is True
        assert status["heat_percent"] == 80
        assert status["total_sends"] >= 2

        # Stop preheating
        await manager.stop()
        assert manager.is_running is False

        # Final status
        final_status = manager.get_status()
        assert final_status["active"] is False

    @pytest.mark.asyncio
    async def test_restart_after_stop(self) -> None:
        """Manager should be restartable after being stopped."""
        driver = _make_mock_driver()
        manager = HeatingManager(driver, send_interval=0.05)

        # First cycle
        await manager.start(heat_percent=80)
        await asyncio.sleep(0.1)
        await manager.stop()

        # Second cycle
        await manager.start(heat_percent=60)
        await asyncio.sleep(0.1)
        assert manager.is_running is True
        assert manager.heat_percent == 60
        await manager.stop()
