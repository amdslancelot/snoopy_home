import aiosqlite
from config import settings


async def db_ping() -> bool:
    """Health-check probe: True when the database answers a trivial query."""
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            await db.execute("SELECT 1")
        return True
    except Exception:
        return False


async def init_db():
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id       INTEGER NOT NULL,
                creator_id       INTEGER NOT NULL,
                target_user_id   INTEGER NOT NULL,
                message          TEXT NOT NULL,
                trigger_time     TEXT NOT NULL,
                is_recurring     INTEGER NOT NULL DEFAULT 0,
                cron_expression  TEXT,
                job_id           TEXT,
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS chore_tasks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id       INTEGER NOT NULL,
                name             TEXT NOT NULL,
                description      TEXT NOT NULL DEFAULT '',
                assigned_user_id INTEGER,
                cron_expression  TEXT NOT NULL,
                last_completed   TEXT,
                job_id           TEXT,
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS todos (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id        INTEGER NOT NULL,
                title             TEXT NOT NULL,
                assigned_user_ids TEXT NOT NULL DEFAULT '[]',
                created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                is_active         INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS household_members (
                discord_id    INTEGER PRIMARY KEY,
                username      TEXT NOT NULL,
                display_name  TEXT NOT NULL,
                timezone      TEXT NOT NULL DEFAULT 'UTC',
                is_active     INTEGER NOT NULL DEFAULT 1
            );
        """)
        await db.commit()

        # Migration: add profile column if not present (safe to run every startup)
        try:
            await db.execute(
                "ALTER TABLE household_members ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'"
            )
            await db.commit()
        except Exception:
            pass  # column already exists

        # Migration: add voice column to reminders
        try:
            await db.execute(
                "ALTER TABLE reminders ADD COLUMN voice INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass  # column already exists
