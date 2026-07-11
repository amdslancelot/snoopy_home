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


import pytest


@pytest.fixture
async def tmp_db(tmp_path, monkeypatch):
    """Initialise a fresh SQLite DB in a temp dir and patch settings to use it."""
    import config
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(config.settings, "db_path", path)
    from storage.database import init_db
    await init_db()
    return path
