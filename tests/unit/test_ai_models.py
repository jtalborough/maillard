"""Model routing and authentication tests; all model requests are mocked."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest
from fastapi import FastAPI, Request

from maillard.ai.agent import RoastAIAgent
from maillard.ai.generate import compute_heuristic_profile, generate_profile
from maillard.core.config import MaillardConfig
from maillard.web.app import _create_ai_client, create_app
from maillard.web.routes import create_router


def test_model_defaults(monkeypatch):
    monkeypatch.delenv("MAILLARD_AI_LIVE_MODEL", raising=False)
    monkeypatch.delenv("MAILLARD_AI_PLAN_MODEL", raising=False)
    config = MaillardConfig(_env_file=None)
    assert config.ai_live_model == "claude-haiku-4-5-20251001"
    assert config.ai_plan_model == "claude-sonnet-4-5-20250929"
    assert config.ai_auto_control_enabled is False


def test_model_env_overrides(monkeypatch):
    monkeypatch.setenv("MAILLARD_AI_LIVE_MODEL", "custom-live")
    monkeypatch.setenv("MAILLARD_AI_PLAN_MODEL", "custom-plan")
    config = MaillardConfig(_env_file=None)
    assert config.ai_live_model == "custom-live"
    assert config.ai_plan_model == "custom-plan"


@pytest.mark.parametrize("oauth", [False, True])
def test_client_authentication(monkeypatch, oauth):
    factory = MagicMock()
    monkeypatch.setattr("anthropic.AsyncAnthropic", factory)
    credential = "sk-ant-oat01-test" if oauth else "sk-ant-api03-test"
    assert _create_ai_client(credential) is factory.return_value
    if oauth:
        factory.assert_called_once_with(
            api_key="", auth_token=credential,
            default_headers={
                "anthropic-beta": "oauth-2025-04-20", "X-Api-Key": anthropic.omit,
            },
        )
    else:
        factory.assert_called_once_with(api_key=credential)


def _state():
    return SimpleNamespace(config=MaillardConfig(
        _env_file=None, ai_live_model="custom-live", ai_plan_model="custom-plan",
    ))


def _request(app):
    return Request({"type": "http", "app": app})


@pytest.mark.asyncio
async def test_live_agent_uses_configured_model():
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(text='{"reasoning": "observe"}')]),
    )))
    agent = RoastAIAgent(_state(), client)
    await agent._call_llm([{"role": "user", "content": "test"}])
    assert client.messages.create.call_args.kwargs["model"] == "custom-live"
    assert agent.get_status()["plan_model"] == "custom-plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "exception", "invalid_json", "invalid_phases"])
async def test_planner_model_and_fallback(monkeypatch, caplog, failure):
    import json

    monkeypatch.setenv("MAILLARD_AI_PLAN_MODEL", "custom-plan")
    monkeypatch.setattr("maillard.ai.generate.load_roast_history", AsyncMock(return_value=None))
    heuristic = compute_heuristic_profile(None)
    text = json.dumps({"phases": heuristic.phases, "charge_temp": heuristic.charge_temp,
                       "target_dtr": heuristic.target_dtr})
    if failure == "invalid_json":
        text = "not JSON"
    elif failure == "invalid_phases":
        text = '{"phases": []}'
    create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text=text)]))
    if failure == "exception":
        create.side_effect = RuntimeError("mock model unavailable")
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with caplog.at_level(logging.WARNING):
        result = await generate_profile("test-coffee", client=client)
    assert create.call_args.kwargs["model"] == "custom-plan"
    if failure:
        assert result.source == "heuristic"
        assert result.ai_model is None
        assert any(record.levelno == logging.WARNING and "heuristic" in record.message
                   for record in caplog.records)
    else:
        assert result.source == "ai"
        assert result.ai_model == "custom-plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
async def test_status_exposes_models_without_network(available):
    state = _state()
    app = FastAPI()
    app.state.maillard = state
    app.state.ai_agent = RoastAIAgent(state, MagicMock()) if available else None
    router = create_router()
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/ai/status")
    status = await endpoint(_request(app))
    assert status["available"] is available
    assert status["live_model"] == "custom-live"
    assert status["plan_model"] == "custom-plan"


def test_app_initialization_uses_live_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-test")
    factory = MagicMock()
    monkeypatch.setattr("anthropic.AsyncAnthropic", factory)
    app = create_app(_state())
    assert app.state.ai_agent._client is factory.return_value
    assert app.state.ai_agent.get_status()["live_model"] == "custom-live"


@pytest.mark.asyncio
async def test_profile_route_uses_plan_model(monkeypatch):
    generator = AsyncMock(return_value=compute_heuristic_profile(None))
    monkeypatch.setattr("maillard.ai.generate.generate_profile", generator)
    state = _state()
    app = FastAPI()
    app.state.maillard = state
    agent = RoastAIAgent(state, MagicMock())
    app.state.ai_agent = agent
    request = _request(app)
    request.json = AsyncMock(return_value={"coffee_id": "test-coffee"})
    router = create_router()
    endpoint = next(route.endpoint for route in router.routes
                    if route.path == "/api/ai/generate-profile")
    result = await endpoint(request)
    assert result["status"] == "ok"
    assert result["profile"]["source"] == "heuristic"
    assert generator.call_args.kwargs["model"] == "custom-plan"
    assert generator.call_args.kwargs["client"] is agent._client


@pytest.mark.asyncio
async def test_pre_roast_plan_uses_plan_model():
    from maillard.ai.history import RoastHistory
    from maillard.core.events import EventBus

    state = _state()
    state.event_bus = EventBus()
    create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text="Observe roast")]))
    agent = RoastAIAgent(state, SimpleNamespace(messages=SimpleNamespace(create=create)))
    agent._roast_history = RoastHistory(
        coffee_name="Test", coffee_origin="Ethiopia", coffee_processing=None,
        coffee_altitude=None, coffee_density=None, past_roasts=[], best_roast=None,
    )
    await agent._generate_pre_roast_plan()
    assert create.call_args.kwargs["model"] == "custom-plan"
