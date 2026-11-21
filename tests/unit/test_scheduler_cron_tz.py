"""Recurring reminders: cron fields are household-local wall-clock."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from integrations.google_calendar import google_calendar
from storage.models import Reminder
from tasks import scheduler


@pytest.fixture
def la(monkeypatch):
    monkeypatch.setattr(google_calendar, "_calendar_timezone", "America/Los_Angeles")
    yield
    for job in scheduler._scheduler.get_jobs():
        job.remove()


def _next_fire(cron: str):
    r = Reminder(id=987, channel_id=1, creator_id=1, target_user_id=1,
                 message="vitamins", trigger_time=datetime.utcnow(),
                 is_recurring=True, cron_expression=cron)
    scheduler.schedule_reminder(r)
    trigger = scheduler._scheduler.get_job("reminder_987").trigger
    return trigger.get_next_fire_time(None, datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc))


def test_weekday_730_fires_at_730_local(la):
    # Fri 2026-11-20 04:00 LA → next is Fri 07:30 LA (was 23:30 LA when cron ran in UTC).
    nxt = _next_fire("30 7 * * 1-5").astimezone(ZoneInfo("America/Los_Angeles"))
    assert (nxt.hour, nxt.minute) == (7, 30)
    assert nxt.weekday() < 5


def test_local_hour_survives_dst_change(la):
    # March 8 2026 is the US DST switch; 8 am must stay 8 am local on both sides.
    _next_fire("0 8 * * *")
    trig = scheduler._scheduler.get_job("reminder_987").trigger
    before = trig.get_next_fire_time(None, datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc))
    after = trig.get_next_fire_time(None, datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc))
    tz = ZoneInfo("America/Los_Angeles")
    assert before.astimezone(tz).hour == 8 and after.astimezone(tz).hour == 8
