-- Initial schema: the SQLite layout ported to Postgres-native types
-- (BIGINT ids for Discord snowflakes, TIMESTAMPTZ, BOOLEAN, JSONB).

CREATE TABLE IF NOT EXISTS reminders (
    id               BIGSERIAL PRIMARY KEY,
    channel_id       BIGINT NOT NULL,
    creator_id       BIGINT NOT NULL,
    target_user_id   BIGINT NOT NULL,
    message          TEXT NOT NULL,
    trigger_time     TIMESTAMPTZ NOT NULL,
    is_recurring     BOOLEAN NOT NULL DEFAULT FALSE,
    cron_expression  TEXT,
    job_id           TEXT,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    voice            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reminders_channel_active_idx ON reminders (channel_id, is_active);

CREATE TABLE IF NOT EXISTS chore_tasks (
    id               BIGSERIAL PRIMARY KEY,
    channel_id       BIGINT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    assigned_user_id BIGINT,
    cron_expression  TEXT NOT NULL,
    last_completed   TIMESTAMPTZ,
    job_id           TEXT,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chore_tasks_channel_active_idx ON chore_tasks (channel_id, is_active);

CREATE TABLE IF NOT EXISTS todos (
    id                BIGSERIAL PRIMARY KEY,
    channel_id        BIGINT NOT NULL,
    title             TEXT NOT NULL,
    assigned_user_ids JSONB NOT NULL DEFAULT '[]',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active         BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS todos_channel_active_idx ON todos (channel_id, is_active);

CREATE TABLE IF NOT EXISTS household_members (
    discord_id    BIGINT PRIMARY KEY,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'UTC',
    profile       JSONB NOT NULL DEFAULT '{}',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);
