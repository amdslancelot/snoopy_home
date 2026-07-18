# Multi-tenancy — households and roles

One bot instance serves any number of Discord servers, each an isolated
household: its own roster, chores, reminders, todos, completion stats, and
per-member profiles. Isolation is enforced in the repository layer — every
query on shared tables carries a `guild_id`.

## Data model

Migration `003_multi_household.sql`:

- `guild_id BIGINT NOT NULL DEFAULT 0` on reminders, chore_tasks, todos,
  chore_completions, household_members.
- `household_members` PK is `(guild_id, discord_id)` — the same person can
  belong to two households with different profiles.
- `user_settings(discord_id PK, home_guild_id)` — which household a DM
  belongs to.
- `guild_id = 0` marks pre-multi-tenancy rows; startup runs
  `backfill_guild_ids(DISCORD_GUILD_ID)` when that setting exists, adopting
  legacy rows into the configured guild (idempotent).

Reminders/chores/todos were already channel-scoped (channel ids are global
snowflakes), so the per-guild column matters most for the cross-channel
paths: the roster, name→profile lookups, `complete_chore` name matching,
`chore_stats`, and group-reminder pings.

## DMs — the home guild

A guild message scopes to its guild. A DM resolves the author's home
household: explicit `/set_home` (or `/register`) wins; a user who shares
exactly one server with the bot is auto-adopted into it; otherwise the bot
asks them to run `/set_home` once.

## Roles

Admin = Discord's **Manage Server** or **Administrator** permission,
computed live per request (`_member_is_admin`) — no stored role column, so
nothing drifts when Discord roles change. Gates, enforced in the tool
executors (single seam for both the LLM path and slash commands):

| Operation | Rule |
|---|---|
| Cancel a reminder | creator, target, or admin (group reminders: anyone) |
| Edit another member's profile | admin only |
| `/remove_chore` | Manage Server permission |
| Everything else | any household member |

Denials return an error dict to the model, which explains politely — the
data stays untouched.

## Prompt-cache economics

The household block (roster + chores) used to live inside the server-side
cached system prompt. Per-guild caches would multiply Gemini's 4096-token
cache minimum by the number of guilds and re-billing on every roster change.
Instead the cache holds only static content (persona, rules, tool schemas),
shared by all guilds and never invalidated by data changes, while each
request prepends a fresh ~100–500-token household message
(`core/household.py`) built from the database. Small uncached tokens per
call beat N_guilds cache storage rents — and the old cache-invalidation
machinery (`update_household`) disappeared entirely.

## Tests

`tests/integration/test_multi_household.py`: migration PK shape, disjoint
rosters, per-guild profiles for the same user, guild-scoped completions and
stats, legacy backfill, home-guild settings, and all role gates (deny +
allow paths at the handler level).
