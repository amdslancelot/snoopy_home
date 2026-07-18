"""
Repository layer — every SQL statement in the app lives here.

Conventions:
- Discord snowflakes are BIGINTs; JSON/JSONB columns arrive as Python
  dicts/lists (codec set in storage/pool.py).
- Datetimes cross this boundary as *naive UTC* (the convention the scheduler
  and dataclasses already use): aware timestamps from Postgres are converted
  on read, naive inputs get UTC attached on write.
- Methods return dataclasses (reminders) or plain dicts (chores, todos,
  members) so callers never touch asyncpg Records.
"""

from datetime import datetime, timezone as _tz
from typing import Optional

import dateparser

from config import settings
from storage.models import Reminder
from storage.pool import pool


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(_tz.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)


# ── Reminders ─────────────────────────────────────────────────────────────────

class ReminderRepository:
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
        row = await pool().fetchrow(
            """INSERT INTO reminders
               (channel_id, creator_id, target_user_id, message,
                trigger_time, is_recurring, cron_expression, voice)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING id""",
            channel_id, creator_id, target_user_id, message,
            _aware_utc(trigger_time), is_recurring, cron_expression, voice,
        )
        return Reminder(
            id=row["id"],
            channel_id=channel_id,
            creator_id=creator_id,
            target_user_id=target_user_id,
            message=message,
            trigger_time=_naive_utc(_aware_utc(trigger_time)),
            is_recurring=is_recurring,
            cron_expression=cron_expression,
            voice=voice,
        )

    async def list_active(self, channel_id: int) -> list[Reminder]:
        rows = await pool().fetch(
            "SELECT * FROM reminders WHERE channel_id=$1 AND is_active ORDER BY trigger_time",
            channel_id,
        )
        return [self._from_row(r) for r in rows]

    async def get_all_active(self) -> list[Reminder]:
        rows = await pool().fetch("SELECT * FROM reminders WHERE is_active")
        return [self._from_row(r) for r in rows]

    async def mark_inactive(self, reminder_id: int):
        await pool().execute("UPDATE reminders SET is_active=FALSE WHERE id=$1", reminder_id)

    async def update_job_id(self, reminder_id: int, job_id: str):
        await pool().execute("UPDATE reminders SET job_id=$1 WHERE id=$2", job_id, reminder_id)

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
        trigger = row["trigger_time"]
        if isinstance(trigger, str):
            trigger = datetime.fromisoformat(trigger)
        return Reminder(
            id=row["id"],
            channel_id=row["channel_id"],
            creator_id=row["creator_id"],
            target_user_id=row["target_user_id"],
            message=row["message"],
            trigger_time=_naive_utc(trigger),
            is_recurring=bool(row["is_recurring"]),
            cron_expression=row["cron_expression"],
            job_id=row["job_id"],
            is_active=bool(row["is_active"]),
            voice=bool(row["voice"]) if row["voice"] is not None else False,
        )


# ── Chores ────────────────────────────────────────────────────────────────────

class ChoreRepository:
    async def create(
        self,
        channel_id: int,
        name: str,
        description: str,
        assigned_user_id: Optional[int],
        cron_expression: str,
    ) -> int:
        row = await pool().fetchrow(
            """INSERT INTO chore_tasks (channel_id, name, description, assigned_user_id, cron_expression)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            channel_id, name, description, assigned_user_id, cron_expression,
        )
        return row["id"]

    async def list_active(self, channel_id: int) -> list[dict]:
        rows = await pool().fetch(
            "SELECT * FROM chore_tasks WHERE channel_id=$1 AND is_active ORDER BY name",
            channel_id,
        )
        return [dict(r) for r in rows]

    async def list_all_active(self) -> list[dict]:
        """All active chores enriched with the assignee's username (for the LLM context)."""
        rows = await pool().fetch(
            """SELECT c.name, c.description, c.cron_expression, c.assigned_user_id,
                      m.username AS assigned_username
               FROM chore_tasks c
               LEFT JOIN household_members m ON m.discord_id = c.assigned_user_id
               WHERE c.is_active"""
        )
        return [dict(r) for r in rows]

    async def complete_by_name(self, name: str) -> int:
        """Mark a chore complete by (case-insensitive) name. Returns rows updated."""
        result = await pool().execute(
            "UPDATE chore_tasks SET last_completed=now() WHERE lower(name)=lower($1) AND is_active",
            name,
        )
        return int(result.split()[-1])

    async def find_active(self, channel_id: int) -> list[tuple[int, str]]:
        rows = await pool().fetch(
            "SELECT id, name FROM chore_tasks WHERE channel_id=$1 AND is_active", channel_id
        )
        return [(r["id"], r["name"]) for r in rows]

    async def deactivate(self, chore_id: int):
        await pool().execute("UPDATE chore_tasks SET is_active=FALSE WHERE id=$1", chore_id)


# ── Todos ─────────────────────────────────────────────────────────────────────

class TodoRepository:
    async def create(self, channel_id: int, title: str, assigned_user_ids: list[int]) -> int:
        row = await pool().fetchrow(
            "INSERT INTO todos (channel_id, title, assigned_user_ids) VALUES ($1, $2, $3) RETURNING id",
            channel_id, title, assigned_user_ids,
        )
        return row["id"]

    async def list_active(self, channel_id: int) -> list[dict]:
        rows = await pool().fetch(
            "SELECT * FROM todos WHERE channel_id=$1 AND is_active ORDER BY created_at",
            channel_id,
        )
        return [dict(r) for r in rows]

    async def find_active(self, channel_id: int) -> list[tuple[int, str]]:
        rows = await pool().fetch(
            "SELECT id, title FROM todos WHERE channel_id=$1 AND is_active", channel_id
        )
        return [(r["id"], r["title"]) for r in rows]

    async def deactivate(self, todo_id: int):
        await pool().execute("UPDATE todos SET is_active=FALSE WHERE id=$1", todo_id)


# ── Household members ─────────────────────────────────────────────────────────

class MemberRepository:
    async def upsert(self, discord_id: int, username: str, display_name: str):
        await pool().execute(
            """INSERT INTO household_members (discord_id, username, display_name)
               VALUES ($1, $2, $3)
               ON CONFLICT (discord_id) DO UPDATE SET
                 username = EXCLUDED.username,
                 display_name = EXCLUDED.display_name""",
            discord_id, username, display_name,
        )

    async def get_profile(self, discord_id: int) -> Optional[dict]:
        row = await pool().fetchrow(
            "SELECT profile FROM household_members WHERE discord_id=$1", discord_id
        )
        return dict(row["profile"]) if row else None

    async def merge_profile(self, discord_id: int, updates: dict) -> bool:
        """Merge keys into the member's profile. False when the member is unknown."""
        result = await pool().execute(
            "UPDATE household_members SET profile = profile || $1::jsonb WHERE discord_id=$2",
            updates, discord_id,
        )
        return result.split()[-1] != "0"

    async def find_profile_by_name(self, name: str) -> Optional[dict]:
        row = await pool().fetchrow(
            """SELECT profile FROM household_members
               WHERE lower(username)=lower($1) OR lower(display_name)=lower($1)""",
            name,
        )
        return dict(row["profile"]) if row else None

    async def active_members(self) -> list[dict]:
        rows = await pool().fetch(
            "SELECT username, display_name, profile FROM household_members WHERE is_active"
        )
        return [dict(r) for r in rows]

    async def active_ids(self) -> list[int]:
        rows = await pool().fetch(
            "SELECT discord_id FROM household_members WHERE is_active"
        )
        return [r["discord_id"] for r in rows]


reminder_repo = ReminderRepository()
chore_repo = ChoreRepository()
todo_repo = TodoRepository()
member_repo = MemberRepository()
