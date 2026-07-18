"""
asyncpg connection pool.

Pool size follows the cluster's connection budget (the shared Postgres
serves several apps; each app keeps its pool at 5-10): min_size=1,
max_size=5. JSON/JSONB columns are transparently encoded/decoded to Python
dicts/lists via a per-connection type codec.
"""

import json
from typing import Optional

import asyncpg

from config import settings

_pool: Optional[asyncpg.Pool] = None


async def _init_conn(conn: asyncpg.Connection):
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url, min_size=1, max_size=5, init=_init_conn
        )
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised — call init_pool() first")
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def db_ping() -> bool:
    """Health-check probe: True when the database answers a trivial query."""
    try:
        await pool().fetchval("SELECT 1")
        return True
    except Exception:
        return False
