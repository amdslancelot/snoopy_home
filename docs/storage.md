# Storage — PostgreSQL

The bot's state lives in PostgreSQL 17, accessed through plain `asyncpg` with
raw SQL. Deliberately no ORM: five tables and one service don't earn
SQLAlchemy's weight, and every query stays greppable.

```
storage/
  pool.py            asyncpg pool (min 1 / max 5 — the shared cluster's
                     per-app connection budget), JSONB↔dict codecs
  migrate.py         ~40-line versioned-migration runner
  migrations/        numbered .sql files, applied in order, tracked in
                     schema_migrations
  repositories.py    ALL SQL in the app: Reminder/Chore/Todo/Member repos
  models.py          plain dataclasses used as row types
scripts/
  migrate_sqlite_to_pg.py   one-time data move from the legacy SQLite file
```

## Local development

```bash
podman run -d --name snoopy-pg \
  -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=snoopy_home \
  -p 5432:5432 postgres:17

# tests use a second database:
psql postgresql://postgres:dev@localhost:5432/postgres -c "CREATE DATABASE snoopy_test"
```

`DATABASE_URL` has no code default (`config.py`) — every environment sets it
via `.env`/secret. Dev conventionally uses
`postgresql://snoopy_rw:dev@localhost:5432/snoopy_home` (see `.env.example`) —
the app connects as the least-privilege `snoopy_rw` role, not the `postgres`
superuser; production supplies its own via the environment (same `snoopy_rw`
role, distinct password, on the shared Postgres in the `data` namespace —
`docs/PLAN-postgres-role-isolation.md`).

## Migrations

Numbered `.sql` files in `storage/migrations/` are applied in filename order
inside transactions; applied versions are recorded in `schema_migrations`,
so re-running is a no-op. They run automatically at startup (`main.py`) —
fine for a single-replica bot — or manually with `python -m storage.migrate`.

To add a migration: create `storage/migrations/NNN_description.sql` with the
next number. Never edit an already-applied file — the runner tracks by
filename, not content.

## Conventions

- **Datetimes**: `TIMESTAMPTZ` in the database; *naive UTC* in Python (the
  convention the scheduler and dataclasses always used). The repository layer
  converts at the boundary — nothing above `storage/` handles tzinfo.
- **JSON**: `JSONB` columns (`household_members.profile`,
  `todos.assigned_user_ids`) cross the boundary as dicts/lists via asyncpg
  type codecs — no manual `json.loads` anywhere above the pool.
- **Profile merges** use `profile || $1::jsonb` server-side — no
  read-modify-write race.
- **All SQL lives in `storage/repositories.py`.** `bot/events.py` contains
  zero SQL (it used to inline it); callers get dataclasses or plain dicts,
  never asyncpg Records.

## Tests

`pg_db` (tests/conftest.py) drops and recreates the `public` schema in
`TEST_DATABASE_URL`, re-runs migrations, and initialises the pool — full
isolation per test against a real Postgres. CI runs a `postgres:17` service
container; locally the fixture skips cleanly when no server is reachable.

## One-time SQLite → Postgres move

```bash
python -m storage.migrate                    # schema first
python scripts/migrate_sqlite_to_pg.py \
  --sqlite snoopy_home.db \
  --pg postgresql://snoopy_rw:<pw>@host:5432/snoopy_home
```

Rows keep their original ids (`ON CONFLICT DO NOTHING` — rerun-safe),
sequences are bumped past the max id, and the script prints per-table
source/dest counts, exiting non-zero on any mismatch. Keep the SQLite file
as the rollback artifact until the cutover has soaked.
