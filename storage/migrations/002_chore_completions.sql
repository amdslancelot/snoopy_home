-- Completion log: chore_tasks.last_completed only keeps the latest timestamp,
-- so questions like "who did the most chores last week" (the chore_stats
-- read tool) need an append-only history.

CREATE TABLE IF NOT EXISTS chore_completions (
    id            BIGSERIAL PRIMARY KEY,
    chore_id      BIGINT NOT NULL REFERENCES chore_tasks(id),
    completed_by  BIGINT,
    completed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chore_completions_at_idx ON chore_completions (completed_at);
