# Plan: Multi-tenant Google Calendar + timezone

Not started — design notes to pick back up later. See `docs/TODO.md` for the
gap this addresses (Calendar/timezone are single global values despite
Phase 5's guild-level DB isolation).

## Onboarding shape (agreed direction)

When the bot joins a new Discord server, a household admin runs a setup
command (e.g. `/setup_calendar <calendar_id>`, admin-only — reuse the
existing Manage-Server admin check). The bot immediately calls the Calendar
API to verify access and auto-detect the timezone (the same way
`GoogleCalendarClient._get_service()` already does today), then persists
both into a new per-guild table:

```
guild_settings(guild_id PK, calendar_id, timezone, ...)
```

No manual timezone entry needed — it's derived from the calendar itself,
just persisted per-guild instead of cached in one global singleton.
`guild_id` is already threaded through `ToolContext` since Phase 5, so
wiring this into `_stamp_date` and the calendar tool executors doesn't need
new plumbing, just a lookup instead of reading `settings.timezone` /
`settings.household_calendar_id` directly.

## Open decision: credential model

Not yet decided. Three options on the table:

**1. One shared service account, households grant it access.**
Keep the single existing service account. Each household shares *their own*
calendar with it and pastes `calendar_id` into `/setup_calendar`. Low
friction, no new GCP project per household. `guild_settings` only needs
`calendar_id` + `timezone`, no credentials stored per-guild.
- Isolation between households is enforced by app code (`guild_id` →
  `calendar_id` lookup) — same trust model as today's shared Postgres
  instance (guild_id column isolation, not a DB-engine-level boundary). A
  bug that skips the guild_id lookup is the same risk class as a missing
  `WHERE guild_id = $1`.
- Caveat: the service-account key is a skeleton key. Whoever holds it (the
  operator) can technically query any calendar ever shared with it,
  bypassing per-guild logic entirely. Households are isolated from each
  other, not from the operator.

**2. Each household brings its own service account/credentials.**
Every household creates their own GCP project + service account, uploads
their own JSON key, stored encrypted per-guild in Postgres (e.g. Fernet +
a master key env var). Real onboarding friction (GCP Console, enabling
Calendar API, generating/downloading a key).
- Does **not** actually remove the operator-trust issue — the operator
  still stores and can decrypt every household's key. Mainly adds friction
  without adding real isolation over option 1.

**3. Per-household Google OAuth (sign-in flow) instead of service accounts.**
Each household admin does a real "Sign in with Google" consent flow; the
bot stores a per-guild OAuth refresh token instead of any shared static
credential.
- Removes the operator-as-skeleton-key issue: each household can
  independently revoke access from their own Google Account permissions
  page, and no single key covers everyone.
- Meaningfully more implementation work: OAuth consent screen, redirect
  handling, refresh-token storage/rotation per guild.

## Next step when picking this back up

Decide which credential model fits the actual intended usage (trusted
friends/family who already trust the operator via shared Postgres, vs.
arms-length households who shouldn't need to) — that decision determines
whether `guild_settings` needs a credentials column at all, or whether
Calendar work is scoped to option 1 (schema + lookup wiring only).
