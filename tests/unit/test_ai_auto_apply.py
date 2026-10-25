"""Focused tests for AI AUTO apply, link pause, and prompt guards."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from maillard.ai.agent import AIMode, RoastAIAgent, _clamp_control, message_contradicted
from maillard.core.config import MaillardConfig
from maillard.core.events import EventBus, SafetyAlertEvent, SafetyAlertLevel


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
    agent._state._heating_manager = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())

    applied = await agent._auto_apply({"heat": 70, "fan": 45.2, "drum": 60})

    assert applied is True
    agent._state._heating_manager.start.assert_awaited_once_with(70)
    agent._state.driver.set_heat.assert_awaited_once_with(70)
    agent._state.driver.set_fan.assert_awaited_once_with(45)
    agent._state.driver.set_drum.assert_awaited_once_with(60)
    assert agent._last_controls == {"heat": 70, "fan": 45, "drum": 60}


@pytest.mark.asyncio
async def test_auto_apply_skips_when_disconnected():
    agent = _agent()
    agent._state.driver.is_connected = False
    applied = await agent._auto_apply({"heat": 70, "fan": 40, "drum": 50})
    assert applied is False
    agent._state.driver.set_drum.assert_not_called()


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        elapsed_time=600.0,
        latest_ror=8.0,
        current_phase=None,
        machine=SimpleNamespace(
            turning_point_time=None,
            dry_end_time=None,
            fc_start_time=None,
        ),
        collector=SimpleNamespace(samples=[], events=[]),
    )


@pytest.mark.asyncio
async def test_operator_override_keeps_auto_and_memory():
    agent = _agent()
    agent._mode = AIMode.AUTO
    agent._state.is_roast_active = True
    agent._locked_plan = {"phases": [{"name": "drop"}]}
    agent._transcript.append({"role": "ai", "content": "keep this"})
    await agent.start()
    task = agent._task

    changed = await agent.notify_operator_override(drum=72)

    assert changed is True
    assert agent.mode == AIMode.AUTO
    assert agent.status_mode == "auto"
    assert agent._fallback_reason is None
    assert agent._last_controls["drum"] == 72
    assert agent._task is task and not task.done()
    assert agent._locked_plan["phases"][0]["name"] == "drop"
    assert any(item["content"] == "keep this" for item in agent._transcript)
    assert any("drum=72%" in item["content"] for item in agent._transcript)
    agent._state._safety_monitor.set_watchdog_enforced.assert_not_called()
    await agent.stop()


@pytest.mark.asyncio
async def test_start_keeps_memory_mid_roast_and_clears_it_when_idle():
    agent = _agent()
    agent._state.is_roast_active = True
    agent._locked_plan = {"phases": []}
    agent._transcript.append({"role": "operator", "content": "color is even"})
    await agent.start()
    assert agent._locked_plan == {"phases": []}
    assert agent._transcript
    await agent.stop()

    agent._state.is_roast_active = False
    agent._locked_plan = {"phases": [{"name": "drop"}]}
    agent._transcript.append({"role": "ai", "content": "stale"})
    await agent.start()
    assert agent._transcript == []
    assert agent._locked_plan is None
    await agent.stop()


@pytest.mark.asyncio
async def test_link_blip_pauses_until_fresh_data_then_resumes():
    agent = _agent()
    agent._mode = AIMode.AUTO
    agent._state.is_roast_active = True
    agent._last_controls = {"heat": 70, "fan": 40, "drum": 55}
    agent._state._heating_manager = SimpleNamespace(start=AsyncMock())
    sub = await agent._state.event_bus.subscribe(SafetyAlertEvent)

    agent._state.driver.is_connected = False
    assert agent._should_fallback_for_link() is True
    assert await agent._handle_link() == "paused"
    assert agent.status_mode == "auto-paused"
    assert agent.mode == AIMode.AUTO
    agent._state.driver.set_heat.assert_not_called()

    agent._state.driver.is_connected = True
    agent._state._monitor.connection_lost = True
    assert await agent._handle_link() == "paused"
    agent._state.driver.set_heat.assert_not_called()

    agent._state._monitor.connection_lost = False
    assert await agent._handle_link() == "resumed"
    assert agent.status_mode == "auto"
    agent._state._heating_manager.start.assert_awaited_once_with(70)
    agent._state.driver.set_heat.assert_awaited_once_with(70)
    agent._state.driver.set_fan.assert_awaited_once_with(40)
    agent._state.driver.set_drum.assert_awaited_once_with(55)
    event = await sub.get(timeout=1.0)
    assert event.condition == "ai_auto_fallback"
    assert event.cleared is True
    await sub.close()


@pytest.mark.asyncio
async def test_long_link_or_drop_stays_manual():
    agent = _agent()
    agent._mode = AIMode.AUTO
    agent._state.is_roast_active = True
    agent._link_paused = True
    agent._link_paused_at = time.monotonic() - 61
    agent._state.driver.is_connected = False
    sub = await agent._state.event_bus.subscribe(SafetyAlertEvent)

    assert await agent._handle_link() == "fallback"
    assert agent.mode == AIMode.OFF
    event = await sub.get(timeout=1.0)
    assert event.level == SafetyAlertLevel.CRITICAL
    assert event.condition == "ai_auto_fallback"
    assert event.cleared is False
    await sub.close()

    agent = _agent()
    agent._mode = AIMode.AUTO
    agent._state.is_roast_active = True
    agent._marked_events.add("fc_start")
    agent._link_paused = True
    agent._link_paused_at = time.monotonic() - 20
    agent._state.driver.is_connected = False
    assert await agent._handle_link() == "paused"
    assert agent.mode == AIMode.AUTO

    agent._link_paused_at = time.monotonic() - 31
    assert await agent._handle_link() == "fallback"
    assert agent.mode == AIMode.OFF

    agent = _agent()
    agent._mode = AIMode.AUTO
    agent._state.is_roast_active = True
    agent._last_controls = {"heat": 70, "fan": 40, "drum": 55}
    agent.block_auto_resume()
    agent._state.driver.is_connected = True
    assert await agent._handle_link() == "blocked"
    agent._state.driver.set_heat.assert_not_called()


@pytest.mark.asyncio
async def test_reenabling_auto_clears_fallback_banner():
    agent = _agent()
    agent._mode = AIMode.AUTO
    await agent._fallback_from_auto("AI loop failed 3 times")
    assert agent.mode == AIMode.OFF
    sub = await agent._state.event_bus.subscribe(SafetyAlertEvent)
    await agent.set_mode(AIMode.AUTO)
    event = await sub.get(timeout=1.0)
    assert event.condition == "ai_auto_fallback"
    assert event.cleared is True
    assert agent.status_mode == "auto"
    await sub.close()
    await agent.stop()


def test_stale_fc_denial_is_dropped_from_the_prompt():
    assert message_contradicted("no FC yet, still drying", {"fc_start"})
    assert message_contradicted("I haven't heard first crack", {"fc_start"})
    assert not message_contradicted("no FC yet", set())
    assert not message_contradicted("color is yellow", {"fc_start"})

    agent = _agent()
    agent._state.roast_session = _session()
    agent._roast_level = "Light"
    agent._batch_grams = 1200
    agent._marked_events.add("fc_start")
    agent._operator_messages = [
        {"timestamp": "10:00", "message": "no FC yet"},
        {"timestamp": "10:20", "message": "color is even"},
    ]
    agent._transcript = [
        {
            "role": "ai",
            "content": "holding through crack",
            "suggestion": {"heat": 70, "fan": 40, "drum": 55},
        }
    ]
    agent._locked_plan = {
        "phases": [
            {
                "name": "drop",
                "target_time_seconds": 720,
                "target_temp": 210,
                "heat_percent": 40,
                "fan_percent": 50,
                "drum_percent": 55,
            }
        ],
        "target_drop_temp": 210,
        "warnings": [],
    }
    built = agent._build_messages()
    assert built is not None
    text = built[0]["content"]
    assert "Drop target is 201C" in text
    assert "After FC, heat may only stay the same or decrease." in text
    assert "Previous decisions" in text
    assert "heat 70%" in text
    assert "rejected" in text
    assert "Use drop target 201C." in text
    assert "no FC yet" not in text
    assert "color is even" in text
