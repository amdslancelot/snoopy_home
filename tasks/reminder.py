from datetime import datetime
from typing import Optional

import aiosqlite
import dateparser

from config import settings
from storage.models import Reminder


class ReminderManager:
    def __init__(self, db_path: str = settings.db_path):
        self.db_path = db_path

    async def create(
        self,
        channel_id: int,
        creator_id: int,
        target_user_id: int,
        message: str,
        trigger_time: datetime,
        is_recurring: bool = False,
        cron_expression: Optional[str] = None,
        voice: bool = False,
    ) -> Reminder:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO reminders
                   (channel_id, creator_id, target_user_id, message,
                    trigger_time, is_recurring, cron_expression, voice)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    channel_id, creator_id, target_user_id, message,
                    trigger_time.isoformat(),
                    int(is_recurring), cron_expression, int(voice),
                ),
            )
            await db.commit()
            return Reminder(
                id=cur.lastrowid,
                channel_id=channel_id,
                creator_id=creator_id,
                target_user_id=target_user_id,
                message=message,
                trigger_time=trigger_time,
                is_recurring=is_recurring,
                cron_expression=cron_expression,
                voice=voice,
            )

    async def list_active(self, channel_id: int) -> list[Reminder]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reminders WHERE channel_id=? AND is_active=1 ORDER BY trigger_time",
                (channel_id,),
            ) as cur:
                return [self._from_row(r) for r in await cur.fetchall()]

    async def get_all_active(self) -> list[Reminder]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM reminders WHERE is_active=1") as cur:
                return [self._from_row(r) for r in await cur.fetchall()]

    async def mark_inactive(self, reminder_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE reminders SET is_active=0 WHERE id=?", (reminder_id,))
            await db.commit()

    async def update_job_id(self, reminder_id: int, job_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE reminders SET job_id=? WHERE id=?", (job_id, reminder_id))
            await db.commit()

    @staticmethod
    def parse_datetime(text: str, tz: str = settings.timezone) -> Optional[datetime]:
        return dateparser.parse(
            text,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": tz,
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )

    @staticmethod
    def _from_row(row) -> Reminder:
        return Reminder(
            id=row["id"],
            channel_id=row["channel_id"],
            creator_id=row["creator_id"],
            target_user_id=row["target_user_id"],
            message=row["message"],
            trigger_time=datetime.fromisoformat(row["trigger_time"]),
            is_recurring=bool(row["is_recurring"]),
            cron_expression=row["cron_expression"],
            job_id=row["job_id"],
            is_active=bool(row["is_active"]),
            voice=bool(row["voice"]) if row["voice"] is not None else False,
        )


reminder_manager = ReminderManager()
