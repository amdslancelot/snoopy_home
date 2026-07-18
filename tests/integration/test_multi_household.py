"""
Multi-household (guild) isolation and role gates against a real Postgres.

Two guilds sharing one bot must have fully disjoint rosters, chores, stats,
and profiles; destructive tool paths must respect the admin gate.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import asyncpg

from storage.repositories import (
    backfill_guild_ids,
    chore_repo,
    member_repo,
    reminder_repo,
    user_settings_repo,
)

GUILD_A, GUILD_B = 1001, 2002


# ── Schema (migration 003) ────────────────────────────────────────────────────

class TestMigration003:
    async def test_household_members_pk_is_guild_scoped(self, pg_db):
        conn = await asyncpg.connect(pg_db)
        try:
            rows = await conn.fetch(
                """SELECT a.attname
                   FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = 'household_members'::regclass AND i.indisprimary"""
            )
        finally:
            await conn.close()
        assert {r["attname"] for r in rows} == {"guild_id", "discord_id"}

    async def test_user_settings_table_exists(self, pg_db):
        assert await user_settings_repo.get_home_guild(1) is None


# ── Guild isolation ───────────────────────────────────────────────────────────

class TestGuildIsolation:
    async def test_rosters_are_disjoint(self, pg_db):
        await member_repo.upsert(GUILD_A, 1, "alice", "Alice")
        await member_repo.upsert(GUILD_B, 2, "bob", "Bob")
        assert [m["username"] for m in await member_repo.active_members(GUILD_A)] == ["alice"]
        assert [m["username"] for m in await member_repo.active_members(GUILD_B)] == ["bob"]

    async def test_same_user_has_separate_profiles_per_guild(self, pg_db):
        await member_repo.upsert(GUILD_A, 7, "carol", "Carol")
        await member_repo.upsert(GUILD_B, 7, "carol", "Carol")
        await member_repo.merge_profile(GUILD_A, 7, {"diet": "vegan"})
        assert await member_repo.get_profile(GUILD_A, 7) == {"diet": "vegan"}
        assert await member_repo.get_profile(GUILD_B, 7) == {}

    async def test_chore_completion_scoped_by_guild(self, pg_db):
        await chore_repo.create(10, "Dishes", "", None, "0 21 * * *", guild_id=GUILD_A)
        await chore_repo.create(20, "Dishes", "", None, "0 21 * * *", guild_id=GUILD_B)
        assert await chore_repo.complete_by_name("dishes", 1, GUILD_A) == 1
        chores_b = await chore_repo.list_all_active(GUILD_B)
        assert len(chores_b) == 1  # B's chore untouched
        rows_b = await chore_repo.list_active(20)
        assert rows_b[0]["last_completed"] is None

    async def test_stats_scoped_by_guild(self, pg_db):
        await member_repo.upsert(GUILD_A, 1, "alice", "Alice")
        await chore_repo.create(10, "Vacuum", "", None, "0 11 * * 6", guild_id=GUILD_A)
        await chore_repo.complete_by_name("vacuum", 1, GUILD_A)
        assert await chore_repo.stats(GUILD_A) != []
        assert await chore_repo.stats(GUILD_B) == []

    async def test_backfill_adopts_legacy_rows(self, pg_db):
        await member_repo.upsert(0, 5, "dave", "Dave")  # pre-migration shape
        await chore_repo.create(30, "Laundry", "", None, "0 9 * * 0")  # guild_id=0
        await backfill_guild_ids(GUILD_A)
        assert [m["username"] for m in await member_repo.active_members(GUILD_A)] == ["dave"]
        assert await member_repo.active_members(0) == []
        assert len(await chore_repo.list_all_active(GUILD_A)) == 1


# ── User settings (DM home guild) ─────────────────────────────────────────────

class TestUserSettings:
    async def test_set_and_get(self, pg_db):
        await user_settings_repo.set_home_guild(42, GUILD_A)
        assert await user_settings_repo.get_home_guild(42) == GUILD_A

    async def test_overwrite(self, pg_db):
        await user_settings_repo.set_home_guild(42, GUILD_A)
        await user_settings_repo.set_home_guild(42, GUILD_B)
        assert await user_settings_repo.get_home_guild(42) == GUILD_B


# ── Role gates (handler level) ────────────────────────────────────────────────

def _fake_source(author_id: int, members: list | None = None):
    guild = SimpleNamespace(members=members or []) if members is not None else None
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, mention=f"<@{author_id}>"),
        guild=guild,
        channel=SimpleNamespace(id=10),
    )


class TestRoleGates:
    async def test_cancel_someone_elses_reminder_denied(self, pg_db):
        from bot.events import _do_cancel_reminder

        r = await reminder_repo.create(
            10, 1, 1, "Alice's reminder", datetime.utcnow() + timedelta(hours=1), guild_id=GUILD_A
        )
        result = await _do_cancel_reminder(
            {"reminder_id": r.id}, _fake_source(99), notify=False,
            guild_id=GUILD_A, is_admin=False,
        )
        assert result["ok"] is False
        assert "permission" in result["error"]
        assert (await reminder_repo.get(r.id)).is_active

    async def test_cancel_own_reminder_allowed(self, pg_db):
        from bot.events import _do_cancel_reminder

        r = await reminder_repo.create(
            10, 99, 99, "mine", datetime.utcnow() + timedelta(hours=1), guild_id=GUILD_A
        )
        result = await _do_cancel_reminder(
            {"reminder_id": r.id}, _fake_source(99), notify=False,
            guild_id=GUILD_A, is_admin=False,
        )
        assert result["ok"] is True
        assert not (await reminder_repo.get(r.id)).is_active

    async def test_admin_can_cancel_anyones_reminder(self, pg_db):
        from bot.events import _do_cancel_reminder

        r = await reminder_repo.create(
            10, 1, 1, "Alice's reminder", datetime.utcnow() + timedelta(hours=1), guild_id=GUILD_A
        )
        result = await _do_cancel_reminder(
            {"reminder_id": r.id}, _fake_source(99), notify=False,
            guild_id=GUILD_A, is_admin=True,
        )
        assert result["ok"] is True

    async def test_editing_another_members_profile_requires_admin(self, pg_db):
        from bot.events import _do_update_profile

        await member_repo.upsert(GUILD_A, 555, "alice", "Alice")
        alice = SimpleNamespace(display_name="Alice", name="alice", id=555)
        action = {"target_user": "@alice", "updates": {"diet": "vegan"}}

        denied = await _do_update_profile(
            action, _fake_source(99, members=[alice]), notify=False,
            guild_id=GUILD_A, is_admin=False,
        )
        assert denied["ok"] is False and "permission" in denied["error"]
        assert await member_repo.get_profile(GUILD_A, 555) == {}

        allowed = await _do_update_profile(
            action, _fake_source(99, members=[alice]), notify=False,
            guild_id=GUILD_A, is_admin=True,
        )
        assert allowed["ok"] is True
        assert await member_repo.get_profile(GUILD_A, 555) == {"diet": "vegan"}

    async def test_editing_own_profile_needs_no_admin(self, pg_db):
        from bot.events import _do_update_profile

        await member_repo.upsert(GUILD_A, 99, "dave", "Dave")
        result = await _do_update_profile(
            {"target_user": None, "updates": {"age": 30}}, _fake_source(99), notify=False,
            guild_id=GUILD_A, is_admin=False,
        )
        assert result["ok"] is True
