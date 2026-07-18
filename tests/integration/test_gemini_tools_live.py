"""
Live tests for native function calling against the real Gemini API.

Covers the two runtime assumptions unit tests cannot verify:
  1. A server-side cache can be created WITH tool declarations baked in,
     and a generate call using cached_content (without re-passing tools /
     system_instruction) still produces function calls.
  2. The tools-variant system prompt still clears the 4096-token context
     cache floor after the legacy action-protocol section was removed.

Skipped without a real GEMINI_API_KEY. Run: pytest -m live tests/integration/test_gemini_tools_live.py -v
"""

import pytest

pytestmark = pytest.mark.live

from config import settings as _settings

_REAL_KEY = (
    bool(_settings.gemini_api_key)
    and _settings.gemini_api_key != "test-gemini-api-key"
)
skip_if_no_key = pytest.mark.skipif(
    not _REAL_KEY, reason="Requires a real GEMINI_API_KEY in .env"
)


def _populate_registry():
    from core.tools import read_tools
    from core.tools.declarations import DECLARATIONS
    from core.tools.registry import registry

    if not registry.names():
        read_tools.register(registry)

    async def _stub(args, ctx):
        return {"ok": True}

    for name, decl in DECLARATIONS.items():
        if name not in registry.names():
            registry.register(decl, _stub)


@skip_if_no_key
class TestGeminiToolsLive:
    @pytest.fixture(autouse=True)
    def _tools_protocol(self, monkeypatch):
        monkeypatch.setattr(_settings, "action_protocol", "tools")
        _populate_registry()

    @pytest.fixture
    def client(self):
        from core.gemini_client import GeminiClient

        return GeminiClient()

    async def test_tools_prompt_clears_cache_token_floor(self, client):
        count = client._client.models.count_tokens(
            model=_settings.model_low, contents=client._full_system_prompt()
        )
        assert count.total_tokens >= 4096, (
            f"tools prompt is {count.total_tokens} tokens — below the 4096 "
            "context-cache floor; Section 6 padding no longer suffices"
        )

    async def test_cache_created_with_tools(self, client):
        await client._ensure_cache(_settings.model_low)
        handle = client._caches[_settings.model_low]
        assert handle.valid(), "cache creation with tools in CreateCachedContentConfig failed"

    async def test_cached_request_produces_tool_call(self, client):
        """cached_content request (no inline tools) must still function-call.

        Asks about reminders — the one domain never present in the
        system-prompt household context, so answering without the read tool
        would be ungrounded (chores ARE in context, so a chores question can
        legitimately skip the tool).
        """
        from core.tools.registry import ToolContext

        ctx = ToolContext(channel_id=0, author_id=0, author_name="LiveTest", dry_run=True)
        text, executed = await client.generate_with_tools(
            [{"role": "user", "content": "what reminders do I have right now?"}],
            _settings.model_low,
            ctx,
        )
        assert any(e["type"] == "list_reminders" for e in executed), (
            f"expected a list_reminders call, got {executed} (text: {text[:120]!r})"
        )
        assert isinstance(text, str) and text.strip()

    async def test_uncached_request_produces_tool_call(self, client):
        from core.tools.registry import ToolContext

        ctx = ToolContext(channel_id=0, author_id=0, author_name="LiveTest", dry_run=True)
        _, executed = await client.generate_with_tools(
            [{"role": "user", "content": "remind me to stretch in 45 minutes"}],
            _settings.model_low,
            ctx,
            use_cache=False,
        )
        assert any(e["type"] == "create_reminder" for e in executed)
