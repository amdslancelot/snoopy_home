import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from core.gemini_client import GeminiClient, _CacheHandle


@pytest.fixture
def client():
    with patch("core.gemini_client.genai.Client"):
        return GeminiClient()


# ── _extract_actions ──────────────────────────────────────────────────────────

class TestExtractActions:
    def test_no_action_block(self, client):
        text, actions = client._extract_actions("Hello there!")
        assert text == "Hello there!"
        assert actions == []

    def test_single_action_parsed(self, client):
        raw = 'Done!\n<action>\n{"type":"create_reminder","message":"take meds"}\n</action>'
        text, actions = client._extract_actions(raw)
        assert len(actions) == 1
        assert actions[0]["type"] == "create_reminder"
        assert actions[0]["message"] == "take meds"

    def test_action_tag_stripped_from_display_text(self, client):
        raw = 'Got it!\n<action>{"type":"complete_chore","name":"Vacuum"}</action>'
        text, actions = client._extract_actions(raw)
        assert "<action>" not in text
        assert "</action>" not in text
        assert "Got it!" in text

    def test_malformed_json_skipped(self, client):
        raw = 'Oops\n<action>not { valid json</action>'
        text, actions = client._extract_actions(raw)
        assert actions == []
        assert "Oops" in text

    def test_multiple_actions_all_parsed(self, client):
        raw = (
            'Text\n'
            '<action>{"type":"a","val":1}</action>\n'
            '<action>{"type":"b","val":2}</action>'
        )
        _, actions = client._extract_actions(raw)
        assert len(actions) == 2
        assert {a["type"] for a in actions} == {"a", "b"}

    def test_whitespace_inside_tags_tolerated(self, client):
        raw = 'Ok\n<action>  \n  {"type":"x"}  \n  </action>'
        _, actions = client._extract_actions(raw)
        assert len(actions) == 1
        assert actions[0]["type"] == "x"

    def test_text_before_action_preserved(self, client):
        raw = 'I will remind you.\n<action>{"type":"create_reminder"}</action>'
        text, _ = client._extract_actions(raw)
        assert "I will remind you." in text


# ── _stamp_date ───────────────────────────────────────────────────────────────

class TestStampDate:
    def test_empty_messages_returned_unchanged(self, client):
        assert client._stamp_date([]) == []

    def test_prepends_utc_timestamp_to_last_user_message(self, client):
        messages = [{"role": "user", "content": "remind me to buy milk"}]
        stamped = client._stamp_date(messages)
        content = stamped[0]["content"]
        assert content.startswith("[")
        assert "UTC" in content
        assert "remind me to buy milk" in content

    def test_only_last_user_message_modified(self, client):
        messages = [
            {"role": "user", "content": "first message"},
            {"role": "model", "content": "bot reply"},
            {"role": "user", "content": "second message"},
        ]
        stamped = client._stamp_date(messages)
        assert stamped[0]["content"] == "first message"
        assert stamped[1]["content"] == "bot reply"
        assert "[" in stamped[2]["content"]
        assert "second message" in stamped[2]["content"]

    def test_model_only_messages_not_modified(self, client):
        messages = [{"role": "model", "content": "hello"}]
        stamped = client._stamp_date(messages)
        assert stamped[0]["content"] == "hello"

    def test_original_list_not_mutated(self, client):
        messages = [{"role": "user", "content": "test"}]
        original_content = messages[0]["content"]
        client._stamp_date(messages)
        assert messages[0]["content"] == original_content


# ── update_household ──────────────────────────────────────────────────────────

class TestUpdateHousehold:
    def test_empty_household(self, client):
        client.update_household([], [])
        assert "No members registered yet" in client._household_context
        assert "No chores scheduled yet" in client._household_context

    def test_member_line_without_profile(self, client):
        client.update_household(
            [{"username": "alice", "display_name": "Alice", "profile": "{}"}], []
        )
        line = client._household_context
        assert "Alice (@alice)" in line
        # No profile bracket when profile is empty
        alice_line = [l for l in line.splitlines() if "alice" in l.lower()][0]
        assert "[" not in alice_line

    def test_member_line_with_profile(self, client):
        client.update_household(
            [{"username": "bob", "display_name": "Bob",
              "profile": '{"age": 30, "diet": "vegan"}'}], []
        )
        assert "age: 30" in client._household_context
        assert "diet: vegan" in client._household_context

    def test_chore_line_formatted(self, client):
        client.update_household([], [{
            "name": "Vacuum living room",
            "description": "include corners",
            "cron_expression": "0 11 * * 6",
            "assigned_username": None,
        }])
        assert "Vacuum living room" in client._household_context
        assert "0 11 * * 6" in client._household_context

    def test_cache_invalidated_when_data_changes(self, client):
        # Establish baseline with empty household first
        client.update_household([], [])

        # Now place a fresh cache handle
        handle = _CacheHandle()
        handle.name = "cache_abc"
        handle.expires_at = datetime.utcnow() + timedelta(hours=1)
        client._caches["somemodel"] = handle
        assert handle.valid()

        # Calling with different data must invalidate the cache
        client.update_household(
            [{"username": "alice", "display_name": "Alice", "profile": "{}"}], []
        )
        assert not handle.valid()

    def test_cache_not_invalidated_when_data_same(self, client):
        client.update_household([], [])  # set baseline
        handle = _CacheHandle()
        handle.name = "cache_xyz"
        handle.expires_at = datetime.utcnow() + timedelta(hours=1)
        client._caches["somemodel"] = handle

        client.update_household([], [])  # same data again
        assert handle.valid()


# ── _CacheHandle ──────────────────────────────────────────────────────────────

class TestCacheHandle:
    def test_invalid_when_empty(self):
        assert not _CacheHandle().valid()

    def test_invalid_when_name_missing(self):
        h = _CacheHandle()
        h.expires_at = datetime.utcnow() + timedelta(hours=1)
        assert not h.valid()

    def test_invalid_when_expired(self):
        h = _CacheHandle()
        h.name = "cache_123"
        h.expires_at = datetime.utcnow() - timedelta(seconds=1)
        assert not h.valid()

    def test_invalid_within_90s_of_expiry(self):
        h = _CacheHandle()
        h.name = "cache_123"
        h.expires_at = datetime.utcnow() + timedelta(seconds=60)  # < 90s buffer
        assert not h.valid()

    def test_valid_when_fresh(self):
        h = _CacheHandle()
        h.name = "cache_123"
        h.expires_at = datetime.utcnow() + timedelta(hours=1)
        assert h.valid()

    def test_invalidate_clears_state(self):
        h = _CacheHandle()
        h.name = "cache_123"
        h.expires_at = datetime.utcnow() + timedelta(hours=1)
        h.invalidate()
        assert not h.valid()
        assert h.name is None
        assert h.expires_at is None
