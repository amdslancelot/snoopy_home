-- Multi-household (guild) isolation.
--
-- guild_id lands on every data table (DEFAULT 0 = the pre-migration single
-- household; startup backfills real rows to DISCORD_GUILD_ID when set, see
-- storage/repositories.py backfill_guild_ids). household_members becomes a
-- per-guild roster: PK (guild_id, discord_id). user_settings maps a Discord
-- user to their home guild so DMs can be scoped.

ALTER TABLE reminders         ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE chore_tasks       ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE todos             ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE chore_completions ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE household_members ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;

ALTER TABLE household_members DROP CONSTRAINT household_members_pkey;
ALTER TABLE household_members ADD PRIMARY KEY (guild_id, discord_id);

CREATE INDEX IF NOT EXISTS chore_tasks_guild_idx ON chore_tasks (guild_id, is_active);
CREATE INDEX IF NOT EXISTS chore_completions_guild_idx ON chore_completions (guild_id, completed_at);

CREATE TABLE IF NOT EXISTS user_settings (
    discord_id    BIGINT PRIMARY KEY,
    home_guild_id BIGINT NOT NULL
);
