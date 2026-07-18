"""Tests for GeminiClient.generate_with_tools — the function-calling loop.

The genai client is fully mocked; fake responses carry function_call parts.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.tools.declarations import DECLARATIONS
from core.tools.registry import ToolContext, ToolRegistry


def _fake_response(calls=None, text=None):
    parts = [
        SimpleNamespace(function_call=SimpleNamespace(name=name, args=args))
        for name, args in (calls or [])
    ]
    content = SimpleNamespace(parts=parts, role="model")
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content)],
        usage_metadata=None,
        text=text,
    )


@pytest.fixture
def client():
    with patch("core.gemini_client.genai.Client"):
        from core.gemini_client import GeminiClient

        return GeminiClient()


def _registry(executor=None, name="list_chores"):
    reg = ToolRegistry()

    async def default(args, ctx):
        return {"ok": True, "chores": []}

    reg.register(DECLARATIONS[name], executor or default)
    return reg


_CTX = ToolContext(channel_id=1, author_id=2, author_name="Tester")
_MSG = [{"role": "user", "content": "what chores do we have?"}]


async def test_executes_tool_then_returns_final_text(client):
    client._client.models.generate_content.side_effect = [
        _fake_response(calls=[("list_chores", {})]),
        _fake_response(text="Here are your chores."),
    ]
    text, executed = await client.generate_with_tools(
        _MSG, "test-model", _CTX, use_cache=False, registry=_registry()
    )
    assert text == "Here are your chores."
    assert executed == [{"type": "list_chores"}]

    # Second request must carry: user msg, the model's call, the tool response.
    second = client._client.models.generate_content.call_args_list[1].kwargs
    assert len(second["contents"]) == 3
    tool_content = second["contents"][2]
    assert tool_content.role == "tool"
    assert tool_content.parts[0].function_response.name == "list_chores"


async def test_executor_exception_fed_back_as_error(client):
    async def boom(args, ctx):
        raise RuntimeError("db exploded")

    client._client.models.generate_content.side_effect = [
        _fake_response(calls=[("list_chores", {})]),
        _fake_response(text="Sorry, something went wrong."),
    ]
    text, executed = await client.generate_with_tools(
        _MSG, "test-model", _CTX, use_cache=False, registry=_registry(executor=boom)
    )
    assert text == "Sorry, something went wrong."
    assert executed == [{"type": "list_chores"}]
    second = client._client.models.generate_content.call_args_list[1].kwargs
    fr = second["contents"][2].parts[0].function_response
    assert fr.response["ok"] is False
    assert "db exploded" in fr.response["error"]


async def test_iteration_cap_stops_runaway_loop(client):
    client._client.models.generate_content.side_effect = [
        _fake_response(calls=[("list_chores", {})]) for _ in range(10)
    ]
    text, executed = await client.generate_with_tools(
        _MSG, "test-model", _CTX, use_cache=False, registry=_registry(), max_iterations=5
    )
    assert client._client.models.generate_content.call_count == 5
    assert len(executed) == 5
    assert text == "Done!"  # fallback when the model never produced text


async def test_uncached_config_carries_tools_and_system_instruction(client):
    client._client.models.generate_content.side_effect = [_fake_response(text="hi")]
    await client.generate_with_tools(
        _MSG, "test-model", _CTX, use_cache=False, registry=_registry()
    )
    config = client._client.models.generate_content.call_args.kwargs["config"]
    assert config.tools, "uncached request must pass tools inline"
    assert config.system_instruction
    assert config.cached_content is None


async def test_cached_config_omits_tools_and_system_instruction(client, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "action_protocol", "tools")
    from core.gemini_client import _CacheHandle

    handle = _CacheHandle()
    handle.name = "cachedContents/fake"
    handle.expires_at = datetime.utcnow() + timedelta(hours=1)
    client._caches["test-model"] = handle

    client._client.models.generate_content.side_effect = [_fake_response(text="hi")]
    await client.generate_with_tools(
        _MSG, "test-model", _CTX, use_cache=True, registry=_registry()
    )
    config = client._client.models.generate_content.call_args.kwargs["config"]
    assert config.cached_content == "cachedContents/fake"
    assert config.tools is None, "cached request must NOT re-pass tools"
    assert config.system_instruction is None


async def test_multiple_calls_in_one_turn_all_execute(client):
    seen = []

    async def record(args, ctx):
        seen.append(dict(args))
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(DECLARATIONS["create_reminder"], record)
    reg.register(DECLARATIONS["create_chore"], record)

    client._client.models.generate_content.side_effect = [
        _fake_response(
            calls=[
                ("create_reminder", {"message": "stretch"}),
                ("create_chore", {"name": "Fridge", "cron": "0 12 1 * *"}),
            ]
        ),
        _fake_response(text="Both done!"),
    ]
    text, executed = await client.generate_with_tools(
        _MSG, "test-model", _CTX, use_cache=False, registry=reg
    )
    assert text == "Both done!"
    assert [e["type"] for e in executed] == ["create_reminder", "create_chore"]
    assert len(seen) == 2
