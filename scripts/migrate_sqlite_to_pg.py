"""
One-time data move: the legacy SQLite file → PostgreSQL.

Usage (after `python -m storage.migrate` has created the schema):

    python scripts/migrate_sqlite_to_pg.py \
        --sqlite /path/to/snoopy_home.db \
        --pg postgresql://postgres:dev@localhost:5432/chores

Idempotent-ish: rows are inserted with their original ids and ON CONFLICT DO
NOTHING, so re-running cannot duplicate. Prints per-table source/dest counts
at the end — verify they match before retiring the SQLite file (keep it as
the rollback artifact).
"""

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import asyncpg


def _dt(value):
    """ISO-8601 text (naive = UTC by app convention) → aware UTC datetime."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _b(value):
    return bool(value)


async def migrate(sqlite_path: str, pg_url: str) -> None:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    conn = await asyncpg.connect(pg_url)

    try:
        # household_members (first: chores reference usernames via join)
        for r in src.execute("SELECT * FROM household_members"):
            await conn.execute(
                """INSERT INTO household_members
                   (discord_id, username, display_name, timezone, profile, is_active)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                   ON CONFLICT (discord_id) DO NOTHING""",
                r["discord_id"], r["username"], r["display_name"],
                r["timezone"] or "UTC",
                json.dumps(json.loads(r["profile"] or "{}")),
                _b(r["is_active"]),
            )

        for r in src.execute("SELECT * FROM reminders"):
            await conn.execute(
                """INSERT INTO reminders
                   (id, channel_id, creator_id, target_user_id, message, trigger_time,
                    is_recurring, cron_expression, job_id, is_active, voice, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                   ON CONFLICT (id) DO NOTHING""",
                r["id"], r["channel_id"], r["creator_id"], r["target_user_id"],
                r["message"], _dt(r["trigger_time"]), _b(r["is_recurring"]),
                r["cron_expression"], r["job_id"], _b(r["is_active"]),
                _b(r["voice"]), _dt(r["created_at"]) or datetime.now(timezone.utc),
            )

        for r in src.execute("SELECT * FROM chore_tasks"):
            await conn.execute(
                """INSERT INTO chore_tasks
                   (id, channel_id, name, description, assigned_user_id, cron_expression,
                    last_completed, job_id, is_active, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT (id) DO NOTHING""",
                r["id"], r["channel_id"], r["name"], r["description"] or "",
                r["assigned_user_id"], r["cron_expression"], _dt(r["last_completed"]),
                r["job_id"], _b(r["is_active"]), _dt(r["created_at"]) or datetime.now(timezone.utc),
            )

        for r in src.execute("SELECT * FROM todos"):
            await conn.execute(
                """INSERT INTO todos (id, channel_id, title, assigned_user_ids, created_at, is_active)
                   VALUES ($1,$2,$3,$4::jsonb,$5,$6)
                   ON CONFLICT (id) DO NOTHING""",
                r["id"], r["channel_id"], r["title"],
                r["assigned_user_ids"] or "[]",
                _dt(r["created_at"]) or datetime.now(timezone.utc), _b(r["is_active"]),
            )

        # Bump sequences past the migrated ids
        for table in ("reminders", "chore_tasks", "todos"):
            await conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )

        # Verify
        print(f"{'table':20s} {'sqlite':>8s} {'postgres':>9s}")
        ok = True
        for table in ("household_members", "reminders", "chore_tasks", "todos"):
            s = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            p = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
            flag = "" if s == p else "   <-- MISMATCH"
            if s != p:
                ok = False
            print(f"{table:20s} {s:8d} {p:9d}{flag}")
        print("\nOK — row counts match." if ok else "\nFAILED — investigate mismatches above.")
        raise SystemExit(0 if ok else 1)
    finally:
        await conn.close()
        src.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default="snoopy_home.db")
    ap.add_argument("--pg", required=True, help="postgresql:// URL of the target database")
    args = ap.parse_args()
    asyncio.run(migrate(args.sqlite, args.pg))
