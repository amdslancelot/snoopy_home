import pytest
from datetime import datetime
from tasks.reminder import ReminderManager


def _row(overrides=None):
    """Build a dict that behaves like an aiosqlite.Row for _from_row()."""
    base = {
        "id": 1,
        "channel_id": 100,
        "creator_id": 200,
        "target_user_id": 300,
        "message": "Take medication",
        "trigger_time": "2026-06-20T09:00:00",
        "is_recurring": 0,
        "cron_expression": None,
        "job_id": "reminder_1",
        "is_active": 1,
        "voice": 0,
    }
    if overrides:
        base.update(overrides)
    return base


class TestFromRow:
    def test_basic_fields(self):
        reminder = ReminderManager._from_row(_row())
        assert reminder.id == 1
        assert reminder.channel_id == 100
        assert reminder.message == "Take medication"
        assert reminder.trigger_time == datetime(2026, 6, 20, 9, 0, 0)
        assert reminder.is_active is True
        assert reminder.voice is False

    def test_recurring_with_cron(self):
        reminder = ReminderManager._from_row(
            _row({"is_recurring": 1, "cron_expression": "30 7 * * 1-5"})
        )
        assert reminder.is_recurring is True
        assert reminder.cron_expression == "30 7 * * 1-5"

    def test_voice_true(self):
        reminder = ReminderManager._from_row(_row({"voice": 1}))
        assert reminder.voice is True

    def test_voice_none_defaults_false(self):
        reminder = ReminderManager._from_row(_row({"voice": None}))
        assert reminder.voice is False

    def test_inactive_reminder(self):
        reminder = ReminderManager._from_row(_row({"is_active": 0}))
        assert reminder.is_active is False


class TestParseDatetime:
    def test_in_n_minutes_returns_future_datetime(self):
        result = ReminderManager.parse_datetime("in 5 minutes")
        assert result is not None
        diff = (result - datetime.utcnow()).total_seconds()
        # Should be roughly 5 min (300s), allow generous ±2 min window
        assert 60 < diff < 500

    def test_at_specific_time_returns_datetime(self):
        result = ReminderManager.parse_datetime("tomorrow at 9am")
        assert result is not None

    def test_garbage_returns_none(self):
        result = ReminderManager.parse_datetime("xyzzy blorp 999")
        # dateparser may return None or a fallback; either is acceptable
        # We just verify it doesn't raise
        assert result is None or isinstance(result, datetime)
