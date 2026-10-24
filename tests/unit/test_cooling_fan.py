"""Cooling-tray fan (CS) enable, confirmation, status, and auto-off."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maillard.core.cooling import CoolingFanController


def _sim_driver(*, connected: bool = True, cs: int = 0):
    sim_state = SimpleNamespace(cs=cs)

    async def send(tag: str, value: str):
        if tag == "CS":
            sim_state.cs = int(value)

    return SimpleNamespace(
        is_connected=connected,
        _sim_state=sim_state,
        _send_message=AsyncMock(side_effect=send),
        get_state_value=lambda key: sim_state.cs if key == "CS" else None,
        state={"CS": sim_state.cs},
    )


@pytest.mark.asyncio
async def test_cooling_enable_confirms_and_reports_status():
    ctrl = CoolingFanController(duration_seconds=240)
    driver = _sim_driver()

    status = await ctrl.set_enabled(driver, True, auto=True, schedule_off=True)

    assert status["enabled"] is True
    assert status["confirmed"] is True
    assert status["auto"] is True
    assert status["duration_seconds"] == 240
    assert status["remaining_seconds"] is not None
    assert status["remaining_seconds"] <= 240
    driver._send_message.assert_awaited_with("CS", "1")
    await ctrl.stop()


@pytest.mark.asyncio
async def test_cooling_unconfirmed_when_roaster_silent(caplog):
    ctrl = CoolingFanController(duration_seconds=60)
    driver = SimpleNamespace(
        is_connected=True,
        _send_message=AsyncMock(),
        get_state_value=lambda key: 0,
        state={"CS": 0},
    )

    with caplog.at_level("WARNING"):
        status = await ctrl.set_enabled(driver, True, auto=True)

    assert status["enabled"] is True
    assert status["confirmed"] is False
    assert "did not confirm" in caplog.text
    await ctrl.stop()


@pytest.mark.asyncio
async def test_cooling_auto_off_after_duration():
    ctrl = CoolingFanController(duration_seconds=0.05)
    driver = _sim_driver()

    await ctrl.set_enabled(driver, True, auto=True, schedule_off=True)
    await asyncio.sleep(0.2)

    status = ctrl.get_status()
    assert status["enabled"] is False
    assert status["auto"] is False
    assert driver._sim_state.cs == 0


@pytest.mark.asyncio
async def test_operator_off_cancels_auto_timer():
    ctrl = CoolingFanController(duration_seconds=10)
    driver = _sim_driver()

    await ctrl.set_enabled(driver, True, auto=True, schedule_off=True)
    await ctrl.set_enabled(driver, False, auto=False, schedule_off=False)
    await asyncio.sleep(0.05)

    status = ctrl.get_status()
    assert status["enabled"] is False
    assert status["remaining_seconds"] is None
