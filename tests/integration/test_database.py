"""
Integration tests for storage/database.py and tasks/reminder.py.

Uses a real SQLite database in a temp directory. No network calls.
"""

import pytest
import aiosqlite
from datetime import datetime, timedelta

from tasks.reminder import ReminderManager


# ── init_db ───────────────────────────────────────────────────────────────────

class TestInitDb:
    async def test_creates_all_tables(self, tmp_db):
        async with aiosqlite.connect(tmp_db) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {r[0] for r in await cur.fetchall()}
        assert {"reminders", "chore_tasks", "household_members", "todos"} <= tables

    async def test_idempotent_second_call(self, tmp_db, monkeypatch):
        import config
        monkeypatch.setattr(config.settings, "db_path", tmp_db)
        from storage.database import init_db
        await init_db()  # second call must not raise
        await init_db()  # third call too

    async def test_profile_column_exists(self, tmp_db):
        async with aiosqlite.connect(tmp_db) as db:
            async with db.execute("PRAGMA table_info(household_members)") as cur:
                cols = {r[1] for r in await cur.fetchall()}
        assert "profile" in cols

    async def test_voice_column_exists_on_reminders(self, tmp_db):
        async with aiosqlite.connect(tmp_db) as db:
            async with db.execute("PRAGMA table_info(reminders)") as cur:
                cols = {r[1] for r in await cur.fetchall()}
        assert "voice" in cols


# ── ReminderManager ───────────────────────────────────────────────────────────

@pytest.fixture
def mgr(tmp_db):
    return ReminderManager(db_path=tmp_db)


class TestReminderManagerCreate:
    async def test_create_returns_reminder_with_id(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        reminder = await mgr.create(
            channel_id=111, creator_id=222, target_user_id=333,
            message="Buy groceries", trigger_time=trigger,
        )
        assert reminder.id is not None
        assert reminder.id > 0
        assert reminder.message == "Buy groceries"
        assert reminder.is_active is True

    async def test_create_recurring_with_cron(self, mgr):
        reminder = await mgr.create(
            channel_id=111, creator_id=222, target_user_id=333,
            message="Take vitamins", trigger_time=datetime.utcnow(),
            is_recurring=True, cron_expression="30 7 * * 1-5",
        )
        assert reminder.is_recurring is True
        assert reminder.cron_expression == "30 7 * * 1-5"

    async def test_create_voice_reminder(self, mgr):
        reminder = await mgr.create(
            channel_id=111, creator_id=222, target_user_id=333,
            message="Wake up!", trigger_time=datetime.utcnow() + timedelta(hours=1),
            voice=True,
        )
        assert reminder.voice is True

    async def test_multiple_creates_get_distinct_ids(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        r1 = await mgr.create(111, 222, 333, "first", trigger)
        r2 = await mgr.create(111, 222, 333, "second", trigger)
        assert r1.id != r2.id


class TestReminderManagerList:
    async def test_list_active_returns_only_active(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        r1 = await mgr.create(111, 222, 333, "active", trigger)
        r2 = await mgr.create(111, 222, 333, "inactive", trigger)
        await mgr.mark_inactive(r2.id)

        active = await mgr.list_active(111)
        ids = {r.id for r in active}
        assert r1.id in ids
        assert r2.id not in ids

    async def test_list_active_filters_by_channel(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        await mgr.create(111, 222, 333, "channel A", trigger)
        await mgr.create(999, 222, 333, "channel B", trigger)

        results = await mgr.list_active(111)
        assert all(r.channel_id == 111 for r in results)

    async def test_get_all_active_spans_channels(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        await mgr.create(111, 222, 333, "ch A", trigger)
        await mgr.create(999, 222, 333, "ch B", trigger)

        all_active = await mgr.get_all_active()
        channels = {r.channel_id for r in all_active}
        assert {111, 999} <= channels


class TestReminderManagerUpdate:
    async def test_mark_inactive(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        r = await mgr.create(111, 222, 333, "delete me", trigger)
        await mgr.mark_inactive(r.id)

        active = await mgr.list_active(111)
        assert all(x.id != r.id for x in active)

    async def test_update_job_id(self, mgr):
        trigger = datetime.utcnow() + timedelta(hours=1)
        r = await mgr.create(111, 222, 333, "job test", trigger)
        await mgr.update_job_id(r.id, "reminder_42")

        import aiosqlite
        async with aiosqlite.connect(mgr.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT job_id FROM reminders WHERE id=?", (r.id,)
            ) as cur:
                row = await cur.fetchone()
        assert row["job_id"] == "reminder_42"


# ── Household member profile ──────────────────────────────────────────────────

class TestMemberProfile:
    async def test_profile_defaults_to_empty_json(self, tmp_db):
        async with aiosqlite.connect(tmp_db) as db:
            await db.execute(
                "INSERT INTO household_members (discord_id, username, display_name) VALUES (?, ?, ?)",
                (12345, "alice", "Alice"),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT profile FROM household_members WHERE discord_id=12345"
            ) as cur:
                row = await cur.fetchone()
        assert row["profile"] == "{}"

    async def test_profile_can_be_updated(self, tmp_db):
        import json
        async with aiosqlite.connect(tmp_db) as db:
            await db.execute(
                "INSERT INTO household_members (discord_id, username, display_name) VALUES (?, ?, ?)",
                (99999, "bob", "Bob"),
            )
            await db.commit()
            await db.execute(
                "UPDATE household_members SET profile=? WHERE discord_id=99999",
                (json.dumps({"age": 28, "diet": "vegan"}),),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT profile FROM household_members WHERE discord_id=99999"
            ) as cur:
                row = await cur.fetchone()
        profile = json.loads(row["profile"])
        assert profile["age"] == 28
        assert profile["diet"] == "vegan"
