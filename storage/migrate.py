"""
Minimal versioned-migration runner.

Numbered `.sql` files in storage/migrations/ are applied in filename order;
each applied file is recorded in `schema_migrations`, so re-running is a
no-op. Each migration runs inside a transaction.

Run manually with `python -m storage.migrate`; main.py also runs this at
startup (single-replica bot — no concurrent-migrator concern).
"""

import asyncio
import pathlib
from typing import Optional

import asyncpg

from config import settings
from core.observability import get_logger

log = get_logger("migrate")

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


async def run_migrations(database_url: Optional[str] = None) -> int:
    """Apply pending migrations. Returns the number applied."""
    conn = await asyncpg.connect(database_url or settings.database_url)
    try:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version    TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

        count = 0
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", path.name
                )
            log.info("migration_applied", version=path.name)
            count += 1

        log.info("migrations_done", newly_applied=count, total=len(applied) + count)
        return count
    finally:
        await conn.close()


if __name__ == "__main__":
    from core.observability import configure_logging

    configure_logging()
    asyncio.run(run_migrations())
