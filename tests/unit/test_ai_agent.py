"""Tests for live AI agent transcript and prompt context helpers."""

from types import SimpleNamespace

import pytest

from maillard.ai.agent import RoastAIAgent
from maillard.core.events import AITranscriptEvent, EventBus


class _DummyClient:
    pass


def _agent() -> RoastAIAgent:
    state = SimpleNamespace(event_bus=EventBus(), roast_session=None, driver=None)
    return RoastAIAgent(state, _DummyClient(), model="test-model")


@pytest.mark.asyncio
async def test_operator_message_is_recorded_and_published():
    agent = _agent()
    sub = await agent._state.event_bus.subscribe(AITranscriptEvent)

    result = await agent.add_operator_message("  First crack is rolling clearly.  ")

    assert "entry" in result
    assert result["entry"]["role"] == "operator"
    assert result["entry"]["content"] == "First crack is rolling clearly."
    assert agent.get_transcript() == [result["entry"]]

    event = await sub.get(timeout=1.0)
    assert event is not None
    assert event.role == "operator"
    assert event.kind == "message"
    assert event.content == "First crack is rolling clearly."

    await sub.close()


@pytest.mark.asyncio
async def test_operator_message_validation():
    agent = _agent()

    empty = await agent.add_operator_message("   ")
    too_long = await agent.add_operator_message("x" * 1001)

    assert empty == {"error": "Message cannot be empty"}
    assert too_long == {"error": "Message must be 1000 characters or fewer"}
    assert agent.get_transcript() == []


@pytest.mark.asyncio
async def test_operator_message_is_included_in_prompt_snapshot():
    agent = _agent()
    await agent.add_operator_message("Smells grassy and color is still yellow.")

    collector = SimpleNamespace(samples=[], events=[])
    session = SimpleNamespace(
        elapsed_time=120.0,
        latest_ror=None,
        machine=SimpleNamespace(
            turning_point_time=None,
            dry_end_time=None,
            fc_start_time=None,
            fc_end_time=None,
            sc_start_time=None,
        ),
        current_phase=None,
        collector=collector,
    )
    driver = SimpleNamespace(state={"BT": 145.0, "ET": 190.0, "HP": 80, "FC": 40, "RC": 50})
    agent._state.roast_session = session
    agent._state.driver = driver

    messages = agent._build_messages()
    snapshot = agent.get_prompt_snapshot()

    assert messages is not None
    assert snapshot["available"] is True
    assert snapshot["model"] == "test-model"
    assert snapshot["metadata"]["operator_message_count"] == 1
    assert snapshot["metadata"]["curve_sample_count"] == 0
    assert "Operator Observations and Questions" in snapshot["messages"][0]["content"]
    assert "Smells grassy and color is still yellow." in snapshot["messages"][0]["content"]


@pytest.mark.asyncio
async def test_suggestion_action_is_recorded():
    agent = _agent()

    result = await agent.record_suggestion_action(
        "rejected",
        {"heat": 70, "fan": 55, "drop": False},
    )

    assert "entry" in result
    entry = result["entry"]
    assert entry["role"] == "operator"
    assert entry["kind"] == "action"
    assert entry["metadata"]["action"] == "rejected"
    assert "Rejected" in entry["content"]
    assert "heat 70%" in entry["content"]
