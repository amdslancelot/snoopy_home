"""
APScheduler wrapper for reminder and chore scheduling.

Jobs use module-level functions (not closures) so APScheduler can
represent them correctly in the MemoryJobStore.
"""

from datetime import timezone as dt_utc
from typing import Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from storage.models import Reminder

_scheduler = AsyncIOScheduler(timezone="UTC")

# Injected at startup by bot/events.py — avoids a circular import.
_fire_cb: Optional[Callable[..., Awaitable[None]]] = None


def init_scheduler(fire_callback: Callable[..., Awaitable[None]]):
    global _fire_cb
    _fire_cb = fire_callback
    if not _scheduler.running:
        _scheduler.start()


async def _dispatch(
    channel_id: int,
    target_user_id: int,
    message: str,
    reminder_id: int,
    is_recurring: bool,
    voice: bool = False,
):
    if _fire_cb:
        try:
            await _fire_cb(channel_id, target_user_id, message, reminder_id, is_recurring, voice)
        except Exception as exc:
            print(f"[scheduler] _fire_reminder raised an exception: {exc}")


def schedule_reminder(reminder: Reminder) -> str:
    """Add or replace an APScheduler job for this reminder. Returns the job ID."""
    job_id = f"reminder_{reminder.id}"

    if reminder.is_recurring and reminder.cron_expression:
        parts = reminder.cron_expression.split()
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4],
            timezone="UTC",
        )
    else:
        # reminder.trigger_time is always a naive UTC datetime; attach tzinfo
        # explicitly so APScheduler doesn't misinterpret it as local time.
        run_date = reminder.trigger_time.replace(tzinfo=dt_utc.utc)
        trigger = DateTrigger(run_date=run_date)

    _scheduler.add_job(
        _dispatch,
        trigger,
        id=job_id,
        replace_existing=True,
        kwargs={
            "channel_id":     reminder.channel_id,
            "target_user_id": reminder.target_user_id,
            "message":        reminder.message,
            "reminder_id":    reminder.id,
            "is_recurring":   reminder.is_recurring,
            "voice":          reminder.voice,
        },
    )
    return job_id


def unschedule_reminder(reminder_id: int):
    job_id = f"reminder_{reminder_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
