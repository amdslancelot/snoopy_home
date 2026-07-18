"""Tests for the deterministic eval scorer and its matchers."""

from datetime import datetime, timedelta

from evals.adapters import canonicalize_actions, canonicalize_tool_calls
from evals.scorers.deterministic import match_value, normalize_cron, score_case

NOW = datetime(2026, 7, 18, 12, 0, 0)


# ── match_value ───────────────────────────────────────────────────────────────

def test_plain_string_match_is_case_insensitive():
    assert match_value("Vacuum", "vacuum", NOW)[0]
    assert not match_value("vacuum", "mop", NOW)[0]


def test_exists_and_null():
    assert match_value({"$exists": True}, "x", NOW)[0]
    assert not match_value({"$exists": True}, None, NOW)[0]
    assert match_value({"$null": True}, None, NOW)[0]
    assert not match_value({"$null": True}, "x", NOW)[0]


def test_icontains():
    assert match_value({"$icontains": "bin"}, "Take out the BINS", NOW)[0]
    assert not match_value({"$icontains": "bin"}, "water plants", NOW)[0]


def test_future():
    future = (NOW + timedelta(hours=2)).isoformat()
    past = (NOW - timedelta(hours=2)).isoformat()
    assert match_value({"$future": True}, future, NOW)[0]
    assert not match_value({"$future": True}, past, NOW)[0]
    assert not match_value({"$future": True}, "not-a-date", NOW)[0]


def test_relative_minutes_within_tolerance():
    spec = {"$relative_minutes": 10, "$tolerance_minutes": 3}
    assert match_value(spec, (NOW + timedelta(minutes=11)).isoformat(), NOW)[0]
    assert not match_value(spec, (NOW + timedelta(minutes=20)).isoformat(), NOW)[0]


def test_cron_normalization():
    assert normalize_cron("0  11 * * 6") == "0 11 * * 6"
    assert match_value({"$cron": "0 11 * * 6"}, "0  11 * *  6", NOW)[0]
    assert not match_value({"$cron": "0 11 * * 6"}, "0 11 * * 5", NOW)[0]


def test_future_accepts_zulu_suffix():
    assert match_value({"$future": True}, (NOW + timedelta(hours=1)).isoformat() + "Z", NOW)[0]


# ── score_case ────────────────────────────────────────────────────────────────

def _case(expected):
    return {"id": "t", "expected": expected}


def test_score_pass_full():
    case = _case(
        {
            "tier": "low",
            "intent": "set_reminder",
            "actions": [
                {"type": "create_reminder", "args_subset": {"message": {"$icontains": "bin"}}}
            ],
        }
    )
    s = score_case(case, "low", "set_reminder",
                   [{"type": "create_reminder", "message": "take out the bins"}], NOW)
    assert s.passed, s.failures


def test_score_fails_on_wrong_action_type():
    case = _case({"actions": [{"type": "create_reminder"}]})
    s = score_case(case, "low", "general", [{"type": "create_chore"}], NOW)
    assert not s.passed
    assert any("action types" in f for f in s.failures)


def test_score_fails_on_extra_action_when_none_expected():
    case = _case({"actions": [], "forbid_actions": ["create_reminder"]})
    s = score_case(case, "low", "general", [{"type": "create_reminder"}], NOW)
    assert not s.passed


def test_score_fails_on_tier_mismatch():
    case = _case({"tier": "low", "actions": []})
    s = score_case(case, "medium", "general", [], NOW)
    assert not s.passed


def test_score_fails_on_bad_arg():
    case = _case(
        {"actions": [{"type": "create_chore", "args_subset": {"cron": {"$cron": "0 11 * * 6"}}}]}
    )
    s = score_case(case, "low", "set_chore", [{"type": "create_chore", "cron": "0 9 * * 1"}], NOW)
    assert not s.passed
    assert any("cron" in f for f in s.failures)


# ── adapters ──────────────────────────────────────────────────────────────────

def test_canonicalize_actions_drops_junk():
    raw = [{"type": "create_chore"}, "garbage", {"no_type": 1}, None]
    assert canonicalize_actions(raw) == [{"type": "create_chore"}]


def test_canonicalize_tool_calls_maps_name_and_args():
    calls = [{"name": "create_reminder", "args": {"message": "hi"}}]
    assert canonicalize_tool_calls(calls) == [{"type": "create_reminder", "message": "hi"}]
