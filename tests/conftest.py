import os
import pathlib


def _dot_env_has(key: str) -> bool:
    """Return True if .env defines a non-empty value for key."""
    env_file = pathlib.Path(".env")
    if not env_file.exists():
        return False
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return bool(val)
    return False


# Only inject fake values when .env doesn't already have them.
# pydantic-settings prefers env vars over .env, so don't clobber real keys.
if not _dot_env_has("DISCORD_TOKEN"):
    os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
if not _dot_env_has("GEMINI_API_KEY"):
    os.environ.setdefault("GEMINI_API_KEY", "test-gemini-api-key")


import asyncpg
import pytest

# CI provides a postgres:17 service container; locally:
#   podman run -d --name snoopy-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=chores -p 5432:5432 postgres:17
#   psql postgresql://postgres:dev@localhost:5432/postgres -c "CREATE DATABASE snoopy_test"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:dev@localhost:5432/snoopy_test"
)


@pytest.fixture
async def pg_db(monkeypatch):
    """Fresh schema in the test database with the pool initialised against it.

    Skips (rather than fails) when no test Postgres is reachable, so the
    pure-unit part of the suite still runs on machines without one.
    """
    import config

    monkeypatch.setattr(config.settings, "database_url", TEST_DATABASE_URL)

    try:
        conn = await asyncpg.connect(TEST_DATABASE_URL, timeout=5)
    except Exception as exc:
        pytest.skip(f"no test Postgres at {TEST_DATABASE_URL}: {exc}")
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await conn.close()

    from storage.migrate import run_migrations

    await run_migrations(TEST_DATABASE_URL)

    from storage import pool as pool_mod

    await pool_mod.close_pool()
    await pool_mod.init_pool()
    yield TEST_DATABASE_URL
    await pool_mod.close_pool()
