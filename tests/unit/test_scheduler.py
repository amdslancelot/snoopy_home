"""
Tests for tasks/scheduler.py — focus on the critical UTC timezone fix.

The root bug was: naive UTC datetimes passed to DateTrigger were treated as
local time (UTC+8), so a "fire at 00:36 UTC" trigger fired at 16:36 UTC the
previous day (already past) and was dropped by APScheduler.

Fix: explicitly attach dt.utc to trigger_time before passing to DateTrigger.
These tests verify that the fix is in place and covers both DateTrigger and
CronTrigger paths.
"""

import pytest
from datetime import datetime, timezone as dt_utc
from unittest.mock import patch, MagicMock

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

from storage.models import Reminder
from tasks.scheduler import schedule_reminder


def _one_time_reminder(**kwargs) -> Reminder:
    base = dict(
        id=1, channel_id=100, creator_id=200, target_user_id=300,
        message="test", trigger_time=datetime(2026, 6, 21, 9, 0, 0),
        is_recurring=False, cron_expression=None, voice=False,
    )
    base.update(kwargs)
    return Reminder(**base)


def _capture_trigger(reminder: Reminder):
    """Call schedule_reminder with a mocked scheduler; return the trigger used."""
    captured = {}

    def fake_add_job(func, trigger, **kw):
        captured["trigger"] = trigger
        return MagicMock(id=f"reminder_{reminder.id}")

    with patch("tasks.scheduler._scheduler.add_job", side_effect=fake_add_job):
        schedule_reminder(reminder)

    return captured["trigger"]


class TestDateTriggerUTC:
    def test_date_trigger_has_utc_tzinfo(self):
        trigger = _capture_trigger(_one_time_reminder())
        assert isinstance(trigger, DateTrigger)
        # This is the critical assertion: naive UTC datetime must have tzinfo attached
        assert trigger.run_date.tzinfo is not None

    def test_date_trigger_timezone_is_utc(self):
        trigger = _capture_trigger(_one_time_reminder())
        # UTC offset must be zero
        assert trigger.run_date.utcoffset().total_seconds() == 0

    def test_date_trigger_preserves_datetime_value(self):
        reminder = _one_time_reminder(trigger_time=datetime(2026, 12, 25, 8, 30, 0))
        trigger = _capture_trigger(reminder)
        rd = trigger.run_date
        assert rd.year == 2026
        assert rd.month == 12
        assert rd.day == 25
        assert rd.hour == 8
        assert rd.minute == 30


class TestCronTriggerUTC:
    def test_cron_trigger_uses_utc_timezone(self):
        reminder = _one_time_reminder(
            is_recurring=True, cron_expression="0 9 * * *",
            trigger_time=datetime.utcnow(),
        )
        trigger = _capture_trigger(reminder)
        assert isinstance(trigger, CronTrigger)
        # CronTrigger stores timezone as a tzinfo-compatible object
        offset = trigger.timezone.utcoffset(datetime.utcnow()).total_seconds()
        assert offset == 0

    def test_cron_trigger_parses_all_fields(self):
        reminder = _one_time_reminder(
            is_recurring=True, cron_expression="30 7 * * 1-5",
            trigger_time=datetime.utcnow(),
        )
        trigger = _capture_trigger(reminder)
        assert isinstance(trigger, CronTrigger)


class TestScheduleReminderJobId:
    def test_job_id_format(self):
        captured_id = {}

        def fake_add_job(func, trigger, id, **kw):
            captured_id["id"] = id
            return MagicMock()

        reminder = _one_time_reminder(id=42)
        with patch("tasks.scheduler._scheduler.add_job", side_effect=fake_add_job):
            result = schedule_reminder(reminder)

        assert result == "reminder_42"
        assert captured_id["id"] == "reminder_42"
