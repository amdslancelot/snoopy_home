"""
Integration tests for the Postgres storage layer: migrations, repositories.

Runs against a real Postgres (TEST_DATABASE_URL; CI provides a postgres:17
service container). Skips when none is reachable.
"""

from datetime import datetime, timedelta

import asyncpg

from storage.migrate import run_migrations
from storage.pool import pool
from storage.repositories import chore_repo, member_repo, reminder_repo, todo_repo


# ── Migrations ────────────────────────────────────────────────────────────────

class TestMigrations:
    async def test_creates_all_tables(self, pg_db):
        conn = await asyncpg.connect(pg_db)
        try:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
        finally:
            await conn.close()
        tables = {r["table_name"] for r in rows}
        assert {"reminders", "chore_tasks", "todos", "household_members", "schema_migrations"} <= tables

    async def test_rerun_is_noop(self, pg_db):
        assert await run_migrations(pg_db) == 0
        assert await run_migrations(pg_db) == 0


# ── ReminderRepository ────────────────────────────────────────────────────────

class TestReminderRepository:
    async def test_create_returns_reminder_with_id(self, pg_db):
        trigger = datetime.utcnow() + timedelta(hours=1)
        reminder = await reminder_repo.create(
            channel_id=111, creator_id=222, target_user_id=333,
            message="Buy groceries", trigger_time=trigger,
        )
        assert reminder.id is not None and reminder.id > 0
        assert reminder.message == "Buy groceries"
        assert reminder.is_active is True

    async def test_trigger_time_round_trips_as_naive_utc(self, pg_db):
        trigger = datetime(2026, 8, 1, 9, 30, 0)  # naive UTC in
        created = await reminder_repo.create(111, 222, 333, "tz check", trigger)
        fetched = [r for r in await reminder_repo.list_active(111) if r.id == created.id][0]
        assert fetched.trigger_time == trigger  # naive UTC out
        assert fetched.trigger_time.tzinfo is None

    async def test_recurring_with_cron(self, pg_db):
        r = await reminder_repo.create(
            111, 222, 333, "Take vitamins", datetime.utcnow(),
            is_recurring=True, cron_expression="30 7 * * 1-5",
        )
        assert r.is_recurring is True
        assert r.cron_expression == "30 7 * * 1-5"

    async def test_voice_flag(self, pg_db):
        r = await reminder_repo.create(
            111, 222, 333, "Wake up!", datetime.utcnow() + timedelta(hours=1), voice=True
        )
        assert r.voice is True

    async def test_list_active_filters_inactive_and_channel(self, pg_db):
        trigger = datetime.utcnow() + timedelta(hours=1)
        r1 = await reminder_repo.create(111, 222, 333, "active", trigger)
        r2 = await reminder_repo.create(111, 222, 333, "inactive", trigger)
        await reminder_repo.create(999, 222, 333, "other channel", trigger)
        await reminder_repo.mark_inactive(r2.id)

        active = await reminder_repo.list_active(111)
        ids = {r.id for r in active}
        assert r1.id in ids and r2.id not in ids
        assert all(r.channel_id == 111 for r in active)

    async def test_get_all_active_spans_channels(self, pg_db):
        trigger = datetime.utcnow() + timedelta(hours=1)
        await reminder_repo.create(111, 222, 333, "ch A", trigger)
        await reminder_repo.create(999, 222, 333, "ch B", trigger)
        channels = {r.channel_id for r in await reminder_repo.get_all_active()}
        assert {111, 999} <= channels

    async def test_update_job_id(self, pg_db):
        r = await reminder_repo.create(111, 222, 333, "job test", datetime.utcnow() + timedelta(hours=1))
        await reminder_repo.update_job_id(r.id, "reminder_42")
        job_id = await pool().fetchval("SELECT job_id FROM reminders WHERE id=$1", r.id)
        assert job_id == "reminder_42"


# ── ChoreRepository ───────────────────────────────────────────────────────────

class TestChoreRepository:
    async def test_create_and_list(self, pg_db):
        await chore_repo.create(111, "Vacuum", "living room", None, "0 11 * * 6")
        rows = await chore_repo.list_active(111)
        assert rows[0]["name"] == "Vacuum"
        assert rows[0]["last_completed"] is None

    async def test_complete_by_name_case_insensitive(self, pg_db):
        await chore_repo.create(111, "Vacuum Living Room", "", None, "0 11 * * 6")
        assert await chore_repo.complete_by_name("vacuum living room") == 1
        rows = await chore_repo.list_active(111)
        assert rows[0]["last_completed"] is not None

    async def test_list_all_active_joins_assignee_username(self, pg_db):
        await member_repo.upsert(0, 555, "alice", "Alice")
        await chore_repo.create(111, "Dishes", "", 555, "0 21 * * *")
        rows = await chore_repo.list_all_active()
        assert rows[0]["assigned_username"] == "alice"

    async def test_deactivate(self, pg_db):
        cid = await chore_repo.create(111, "Old chore", "", None, "0 9 * * 1")
        await chore_repo.deactivate(cid)
        assert await chore_repo.list_active(111) == []


# ── TodoRepository ────────────────────────────────────────────────────────────

class TestTodoRepository:
    async def test_create_and_list_jsonb_ids(self, pg_db):
        await todo_repo.create(111, "Buy paint", [1, 2])
        rows = await todo_repo.list_active(111)
        assert rows[0]["title"] == "Buy paint"
        assert rows[0]["assigned_user_ids"] == [1, 2]  # JSONB → list, no json.loads

    async def test_deactivate(self, pg_db):
        tid = await todo_repo.create(111, "Old todo", [])
        await todo_repo.deactivate(tid)
        assert await todo_repo.list_active(111) == []


# ── MemberRepository ──────────────────────────────────────────────────────────

class TestMemberRepository:
    async def test_profile_defaults_to_empty_dict(self, pg_db):
        await member_repo.upsert(0, 12345, "alice", "Alice")
        assert await member_repo.get_profile(0, 12345) == {}

    async def test_merge_profile_merges_not_replaces(self, pg_db):
        await member_repo.upsert(0, 99999, "bob", "Bob")
        assert await member_repo.merge_profile(0, 99999, {"age": 28}) is True
        assert await member_repo.merge_profile(0, 99999, {"diet": "vegan"}) is True
        assert await member_repo.get_profile(0, 99999) == {"age": 28, "diet": "vegan"}

    async def test_merge_profile_unknown_member_returns_false(self, pg_db):
        assert await member_repo.merge_profile(0, 424242, {"x": 1}) is False

    async def test_upsert_updates_names(self, pg_db):
        await member_repo.upsert(0, 777, "old", "Old Name")
        await member_repo.upsert(0, 777, "new", "New Name")
        members = await member_repo.active_members(0)
        me = [m for m in members if m["username"] == "new"]
        assert me and me[0]["display_name"] == "New Name"

    async def test_find_profile_by_name_matches_display_name(self, pg_db):
        await member_repo.upsert(0, 888, "alice", "Ali")
        await member_repo.merge_profile(0, 888, {"google_email": "a@example.com"})
        profile = await member_repo.find_profile_by_name(0, "ali")
        assert profile == {"google_email": "a@example.com"}

    async def test_active_ids(self, pg_db):
        await member_repo.upsert(0, 1, "a", "A")
        await member_repo.upsert(0, 2, "b", "B")
        assert set(await member_repo.active_ids(0)) == {1, 2}
