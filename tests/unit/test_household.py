"""Tests for core/household.py — the per-request household block formatter
(ported from the old GeminiClient.update_household, which baked this into
the cached system prompt; multi-tenancy moved it to a per-request message)."""

from core.household import PREAMBLE, format_household


def test_empty_household():
    block = format_household([], [])
    assert "No members registered yet" in block
    assert "No chores scheduled yet" in block


def test_member_line_without_profile():
    block = format_household(
        [{"username": "alice", "display_name": "Alice", "profile": "{}"}], []
    )
    assert "Alice (@alice)" in block
    alice_line = [l for l in block.splitlines() if "alice" in l.lower()][0]
    assert "[" not in alice_line  # no profile bracket when profile is empty


def test_member_profile_as_json_string():
    block = format_household(
        [{"username": "bob", "display_name": "Bob", "profile": '{"age": 30, "diet": "vegan"}'}],
        [],
    )
    assert "age: 30" in block
    assert "diet: vegan" in block


def test_member_profile_as_dict():
    # JSONB codec delivers dicts — both forms must work
    block = format_household(
        [{"username": "bob", "display_name": "Bob", "profile": {"age": 30}}], []
    )
    assert "age: 30" in block


def test_chore_line_formatted():
    block = format_household([], [{
        "name": "Vacuum living room",
        "description": "include corners",
        "cron_expression": "0 11 * * 6",
        "assigned_username": None,
    }])
    assert "Vacuum living room" in block
    assert "0 11 * * 6" in block


def test_chore_assignee_rendered():
    block = format_household([], [{
        "name": "Dishes",
        "description": "",
        "cron_expression": "0 21 * * *",
        "assigned_username": "alice",
    }])
    assert "assigned to @alice" in block


def test_preamble_marks_system_origin():
    assert "not a user message" in PREAMBLE
