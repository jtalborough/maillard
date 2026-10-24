"""Focused tests for AI AUTO heat/fan/drum apply and safety fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.ai.agent import AIMode, RoastAIAgent, _clamp_control
from maillard.core.config import MaillardConfig
from maillard.core.events import EventBus, SafetyAlertEvent


class _DummyClient:
    pass


def _agent(*, auto_enabled: bool = True) -> RoastAIAgent:
    config = MaillardConfig(ai_auto_control_enabled=auto_enabled)
    driver = SimpleNamespace(
        is_connected=True,
        set_heat=AsyncMock(),
        set_fan=AsyncMock(),
        set_drum=AsyncMock(),
        state={"BT": 180.0, "ET": 200.0, "HP": 50, "FC": 40, "RC": 55},
    )
    state = SimpleNamespace(
        config=config,
        event_bus=EventBus(),
        roast_session=None,
        driver=driver,
        _heating_manager=None,
        _safety_monitor=SimpleNamespace(
            reset_watchdog=MagicMock(),
            set_watchdog_enforced=MagicMock(),
        ),
        _monitor=SimpleNamespace(connection_lost=False),
        is_roast_active=True,
    )
    return RoastAIAgent(state, _DummyClient(), model="test-model")


def test_clamp_control_bounds_and_floats():
    assert _clamp_control(55.6) == 56
    assert _clamp_control(-5) == 0
    assert _clamp_control(140) == 100
    assert _clamp_control(None) is None
    assert _clamp_control(True) is None


@pytest.mark.asyncio
async def test_auto_apply_sets_heat_fan_and_drum():
    agent = _agent()
    agent._mode = AIMode.AUTO

    applied = await agent._auto_apply({"heat": 70, "fan": 45.2, "drum": 60})

    assert applied is True
    agent._state.driver.set_heat.assert_awaited_once_with(70)
    agent._state.driver.set_fan.assert_awaited_once_with(45)
    agent._state.driver.set_drum.assert_awaited_once_with(60)


@pytest.mark.asyncio
async def test_auto_apply_skips_when_disconnected():
    agent = _agent()
    agent._state.driver.is_connected = False
    applied = await agent._auto_apply({"heat": 70, "fan": 40, "drum": 50})
    assert applied is False
    agent._state.driver.set_drum.assert_not_called()


@pytest.mark.asyncio
async def test_operator_override_falls_back_from_auto():
    agent = _agent()
    agent._mode = AIMode.AUTO
    sub = await agent._state.event_bus.subscribe(SafetyAlertEvent)

    changed = await agent.notify_operator_override(drum=72)

    assert changed is True
    assert agent.mode == AIMode.OFF
    assert agent._fallback_reason and "drum=72%" in agent._fallback_reason
    agent._state._safety_monitor.set_watchdog_enforced.assert_called_with(False)
    event = await sub.get(timeout=1.0)
    assert event is not None
    assert event.condition == "ai_auto_fallback"
    assert "manual" in event.message.lower()
    await sub.close()


@pytest.mark.asyncio
async def test_disconnect_triggers_auto_fallback():
    agent = _agent()
    agent._mode = AIMode.AUTO
    agent._state.driver.is_connected = False

    assert agent._should_fallback_for_link() is True
    await agent._fallback_from_auto("Roaster disconnected or BT data stale")
    assert agent.mode == AIMode.OFF
