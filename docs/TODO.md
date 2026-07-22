# TODO

## Google Calendar + timezone aren't multi-tenant

Despite Phase 5's guild-level DB isolation (`docs/multi-tenancy.md`),
Google Calendar integration and timezone resolution are still single-tenant:

- `HOUSEHOLD_CALENDAR_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, and `TIMEZONE` are
  single global values (`config.py`), not per-guild.
- `integrations/google_calendar.py`'s `google_calendar` client is one
  process-wide singleton with one cached timezone — every guild on one bot
  deployment shares the same calendar and the same detected timezone.
- The per-member `timezone` DB column (`storage/migrations/001_initial.sql`)
  and the `update_profile` tool's `timezone` key are both dead — never read
  back by `parse_datetime` (`storage/repositories.py:94`), `_stamp_date`
  (`core/gemini_client.py`), or anywhere else.

**Fix needs:** a per-guild config table (`calendar_id`, `timezone` columns)
before two households in different real-world timezones can use one bot
deployment correctly. Design notes: `docs/PLAN-multi-tenant-calendar.md`.
