"""
Live integration tests against the real Gemini API.

These tests make actual API calls and consume quota. They are skipped unless
a real GEMINI_API_KEY is present (not the test placeholder set in conftest.py).

Run with:
    pytest -m live tests/integration/test_gemini_api.py -v
"""

import os
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


@skip_if_no_key
class TestGeminiAPILive:
    """End-to-end tests against the real Gemini API (model: gemini-2.5-flash-lite).

    These exercise the LEGACY <action> protocol (kept behind
    ACTION_PROTOCOL=legacy for one release); the native function-calling
    path is covered by test_gemini_tools_live.py.
    """

    @pytest.fixture(autouse=True)
    def _legacy_protocol(self, monkeypatch):
        monkeypatch.setattr(_settings, "action_protocol", "legacy")

    @pytest.fixture
    def client(self):
        from unittest.mock import patch
        # Use real API client — no mocking
        from core.gemini_client import GeminiClient
        c = GeminiClient()
        c.update_household([], [])
        return c

    @pytest.fixture
    def model(self):
        from config import settings
        return settings.model_low  # cheapest model for tests

    def _msg(self, text: str) -> list[dict]:
        return [{"role": "user", "content": f"[2026-06-20 12:00:00 UTC]\n{text}"}]

    async def test_basic_generation_returns_text(self, client, model):
        text, actions = await client.generate(
            self._msg("Say 'hello' in exactly one word."),
            model=model,
            use_cache=False,
        )
        assert len(text) > 0

    async def test_reminder_request_emits_create_reminder_action(self, client, model):
        text, actions = await client.generate(
            self._msg("remind me to take my medication at 3pm today"),
            model=model,
            use_cache=False,
        )
        action_types = [a.get("type") for a in actions]
        assert "create_reminder" in action_types

    async def test_reminder_action_has_required_fields(self, client, model):
        _, actions = await client.generate(
            self._msg("remind me to take my medication at 3pm today"),
            model=model,
            use_cache=False,
        )
        action = next((a for a in actions if a.get("type") == "create_reminder"), None)
        assert action is not None
        assert "message" in action
        assert "datetime" in action or action.get("recurring")

    async def test_chore_request_emits_create_chore_action(self, client, model):
        _, actions = await client.generate(
            self._msg("add a chore — vacuum the living room every Saturday at 11am"),
            model=model,
            use_cache=False,
        )
        action_types = [a.get("type") for a in actions]
        assert "create_chore" in action_types

    async def test_query_returns_text_no_action(self, client, model):
        text, actions = await client.generate(
            self._msg("what chores do you recommend for a small apartment?"),
            model=model,
            use_cache=False,
        )
        assert len(text) > 0
        assert actions == []

    async def test_profile_update_emits_update_profile_action(self, client, model):
        client.update_household(
            [{"username": "alice", "display_name": "Alice", "profile": "{}"}], []
        )
        _, actions = await client.generate(
            self._msg("by the way I'm 28 and I usually wake up at 6:30am"),
            model=model,
            use_cache=False,
        )
        action_types = [a.get("type") for a in actions]
        assert "update_profile" in action_types

    async def test_profile_action_has_updates_dict(self, client, model):
        _, actions = await client.generate(
            self._msg("just so you know, I'm 35 years old"),
            model=model,
            use_cache=False,
        )
        action = next((a for a in actions if a.get("type") == "update_profile"), None)
        if action:  # model may or may not emit for minimal info
            assert isinstance(action.get("updates"), dict)

    async def test_action_block_stripped_from_display_text(self, client, model):
        text, _ = await client.generate(
            self._msg("remind me to call mom tomorrow at 10am"),
            model=model,
            use_cache=False,
        )
        assert "<action>" not in text
        assert "</action>" not in text

    async def test_no_reply_fallback_never_occurs(self, client, model):
        """generate() should always return non-empty text for well-formed requests."""
        text, _ = await client.generate(
            self._msg("remind me to buy milk"),
            model=model,
            use_cache=False,
        )
        assert text.strip() != ""
