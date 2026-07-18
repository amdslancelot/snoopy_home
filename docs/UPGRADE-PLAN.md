# Snoopy Home — Enterprise-Grade Upgrade Plan

Status tracking: `[ ]` pending · `[x]` done. Updated as phases land.

## Context

Snoopy Home (Discord AI home-assistant bot, Python 3.11 / discord.py / google-genai / SQLite) works, but as a portfolio piece it lacks what distinguishes senior candidates: an **eval harness** (the flagship differentiator), **native tool use** (today's `<action>{JSON}</action>` protocol is regex-extracted and write-only), **PostgreSQL** (replacing SQLite — also what the k3s cluster design prescribes: shared Postgres 17, database `chores`, role `chores_rw`, pool 5–10), **multi-tenancy** (single global household, no permissions), and **observability** (bare `print()`, no metrics, no HTTP surface).

Decisions locked: hybrid eval scoring (deterministic + LLM-judge); Gemini native function calling only, **no MCP server**; multi-household guild isolation + admin/member roles; Prometheus + structlog + `/health`; SQLite → PostgreSQL via **plain asyncpg** (no ORM), numbered `.sql` migrations with a mini runner; local dev Postgres via plain `podman run` (no compose).

Phase order rationale: monitoring first (zero risk, instruments everything after); eval second (baselines the legacy action protocol *before* the function-calling migration — the runner captures actions in-process and never touches the DB, so it is storage-agnostic); Postgres third (the storage rewrite lands before function calling so the repository layer is written once, and before multi-household so guild scoping is a plain Postgres migration); function calling fourth; multi-household last (guild scoping + role gates land at the single tool-registry seam instead of 9 inline handlers).

---

## [x] Phase 1 — Monitoring (~500 LOC)

**New:** `core/observability.py` (structlog config — JSON in prod, console dev via `LOG_FORMAT`; Prometheus metric definitions), `web/health.py` (aiohttp on the bot's event loop: `/health` = `bot.is_ready` + DB ping + `scheduler.running`; `/metrics` = Prometheus exposition), `deploy/grafana/snoopy-dashboard.json`, `docs/observability.md`, `tests/unit/test_observability.py`.

**Modified:** `main.py` (start aiohttp site, port 8080), `config.py` (`metrics_port`, `log_format`, price-per-MTok table for the 3 models — needs manual refresh when Google reprices), `Dockerfile` (`EXPOSE 8080`), `deploy/DEPLOY-K3S.md` (containerPort + livenessProbe httpGet `/health`), every `print()` site in `bot/events.py`, `core/gemini_client.py`, `core/llm_router.py`, `tasks/`, `integrations/`.

**Metrics:** `llm_request_duration_seconds{model,tier}`, `llm_tokens_total{model,kind=prompt|candidates|cached|thoughts}` (from `response.usage_metadata`), `llm_cost_usd_total{model}`, `action_executions_total{action,status}`, `router_tier_total{tier}`, `reminders_fired_total`, `discord_events_total{event}`.

**Design:** metrics live in `core/`, import nothing from `bot/` — layering safe. Tests use a fresh `CollectorRegistry` per test and `aiohttp.test_utils`.

**Done when:** zero `print()` remains; `curl :8080/metrics` moves during a live run; dashboard imports into Grafana; existing tests green.

## [x] Phase 2 — Eval harness (~700 LOC + dataset) — flagship
> Landed. Baseline (legacy protocol): 54/55 deterministic (98.2%), judge 4.65/5 — see docs/evals.md.

**New:** `evals/dataset/golden.jsonl` (~60 cases: 9 action types ×3+, read queries, chit-chat/no-action, edge cases — remind-vs-relay, ambiguous/past times, `@everyone`, voice; fields `{id, tags, history, user_message, expected:{tier, intent, actions:[{type, args_subset}], forbid_actions, reply_rubric}}`), `evals/runner.py`, `evals/scorers/deterministic.py`, `evals/scorers/judge.py`, `evals/adapters.py` (action-dict → canonical intent now; tool-call → same after Phase 4, so the golden set survives the migration unchanged), `evals/report.py`, `.github/workflows/eval.yml`, `tests/unit/test_router_eval.py`, `tests/unit/test_eval_scorers.py`, `docs/evals.md`.

**Design:**
- Runner drives the REAL pipeline (analyzer → router → `gemini_client.generate`) with a `FakeExecutor` capturing actions — no Discord objects, no DB writes (storage-agnostic).
- Deterministic scorers with normalizers: action-type set equality; arg-subset match (datetime ±2 min tolerance, cron semantic equality, case-insensitive names); tier match.
- Judge: `gemini-2.5-flash`, temperature 0, `response_schema` → `{score:1-5, rationale}` per-case rubric. Report-only, never a CI gate.
- Router eval is pure rules → ordinary pytest in the existing free CI gate.
- `eval.yml`: nightly + `workflow_dispatch` + `eval` PR label; `GEMINI_API_KEY` secret; gate = deterministic pass-rate ≥ 90%; uploads JSON + markdown report artifact.

**Done when:** baseline report of the legacy `<action>` protocol committed to `docs/evals.md`; nightly workflow green.

## [x] Phase 3 — PostgreSQL replaces SQLite (~700 LOC)
> Landed. Data-move rehearsed on the real snoopy_home.db: counts match, rerun is a no-op.

**Stack:** plain `asyncpg` + raw SQL (placeholders `?` → `$1`), no ORM. Migrations: numbered `.sql` files in `storage/migrations/` applied by a ~30-line runner tracking state in `schema_migrations`. Pool per the k3s budget: `asyncpg.create_pool(min_size=1, max_size=5)`, database `chores`, role `chores_rw`.

**New:** `storage/pool.py`, `storage/repositories.py` (absorbs ReminderManager and ALL inline SQL from `bot/events.py`; dataclasses in `storage/models.py` stay as row types), `storage/migrations/001_initial.sql`, `storage/migrate.py` (`python -m storage.migrate`), `scripts/migrate_sqlite_to_pg.py` (one-time data move, prints per-table row counts), `docs/storage.md`.

**Modified:** `config.py` (`DATABASE_URL` replaces `db_path`; `db_path` kept only as migration-script input), `main.py` (pool init/close), `bot/events.py` (inline SQL → repositories), `tasks/reminder.py` (folded into repositories), `requirements.txt` (+`asyncpg`, −`aiosqlite`), `entrypoint.sh` (run migrations before `main.py`), `tests/conftest.py` (`tmp_db` → `pg_db` fixture), `.github/workflows/deploy.yml` (postgres:17 service container in test job), `deploy/DEPLOY-K3S.md` (talk to `postgres.data.svc:5432/chores` from day one — drop SQLite-on-PVC interim + PVC + DB-file-copy cutover; secrets gain `DATABASE_URL`; staging gets `chores_staging` DB + `chores_staging_rw` role).

**Design:** local dev Postgres: `podman run -d --name snoopy-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=chores -p 5432:5432 postgres:17`. `timestamptz` replaces ISO-text timestamps. Single replica (Recreate) → no advisory locking needed.

**Done when:** bot end-to-end on local podman Postgres; CI green against service container; data-move rehearsed on a prod-DB copy with matching row counts; `aiosqlite` gone.

## [ ] Phase 4 — Native function calling (~800 LOC, net-negative prompt)

**New:** `core/tools/registry.py` (`ToolSpec` = name + `types.FunctionDeclaration` + async executor; `ToolContext` = channel_id, guild, author, reply hook), `core/tools/declarations.py` (15 tools: 9 writes + reads `list_reminders`, `list_chores`, `list_todos`, `get_member_profile`, `list_calendar_events`, `chore_stats`), migration `002_chore_completions.sql` (completion log so "who did most chores last week" is answerable), read queries in `storage/repositories.py`, `tests/unit/test_tool_loop.py`, `tests/unit/test_registry.py`, `docs/function-calling.md`.

**Modified:** `core/gemini_client.py` — `generate_with_tools()`: loop while parts contain `function_call`, cap 5 iterations; append model `function_call` Content + `types.Part.from_function_response`; executor exception → `{"error": ...}` function_response; each iteration instrumented with Phase-1 metrics. `bot/events.py` — `_do_*` bodies become registered executors, injected at startup (preserves core-never-imports-bot). `config.py` — `action_protocol: "tools"|"legacy"` rollback flag.

**Caching:** bake tool declarations INTO the per-model cache via `CreateCachedContentConfig(tools=...)`. Runtime constraint (a `cached_content` request may not also pass `tools`/`system_instruction`) covered by a live-marker test; uncached fallback passes both explicitly. Delete prompt Section 4 (action protocol); keep Section 6 KB as 4096-floor padding; assert with `count_tokens` in the live test.

**Done when:** Phase-2 eval rerun via the tool-call adapter scores ≥ legacy baseline; new golden cases for read tools; legacy regex path deleted one release later.

## [ ] Phase 5 — Multi-household + roles (~500 LOC)

**Migration `003_multi_household.sql`:** `guild_id` on reminders/chore_tasks/todos/chore_completions (NOT NULL DEFAULT 0, backfilled from `settings.discord_guild_id` — documented one-time step); `household_members` PK → `(guild_id, discord_id)`; new `user_settings(discord_id PK, home_guild_id)`. Thread `guild_id` through `bot/events.py`, `storage/repositories.py`, `core/tools/registry.py` (`ToolContext` gains `guild_id`, `is_admin`). `docs/multi-tenancy.md`.

**Key decisions:**
- Roles derive from Discord permissions (`manage_guild`/admin → admin) — no stored column, no drift. Enforced centrally in tool executors: cancelling/deleting others' reminders/chores/events, editing others' profiles.
- DM home-guild: `user_settings.home_guild_id`; auto-set when exactly one mutual guild; else `/set_home` slash command.
- Cache economics: do NOT go per-(guild, model). Household roster/chores leave the cache (cache holds only static persona/rules/tools, shared per-model; `update_household` invalidation disappears); the per-guild household block rides as a leading context message per request.
- `context_manager` per-channel keys already sufficient (channel snowflakes globally unique; DMs have own channel).

**Done when:** two Discord servers on one bot with disjoint rosters/chores; non-admin blocked from destructive tools with a polite reply; migration runner applies cleanly on a fresh DB and re-runs as a no-op.

---

## Top risks

1. `cached_content` + `tools` request mixing rejected by API → tools baked into cache, explicit fallback, live-marker test first.
2. FC behavioral regression → Phase-2 baseline gate + `action_protocol` rollback flag + iteration cap.
3. SQLite→Postgres data-move fidelity (ISO-text → timestamptz, JSON profile, cron strings) → script prints per-table counts + spot-checks; rehearse on prod-DB copy; SQLite file kept as rollback.
4. Eval flakiness/cost → tolerant normalizers, 90% threshold, judge report-only, live evals nightly not per-push.
5. Prompt below 4096 cache floor after Section-4 removal → `count_tokens` live check; Section-6 padding retained; cache-create failure already falls back transparently.

## Verification

- Per phase: full pytest suite green; CI passes.
- Phase 1: run bot locally, `curl :8080/health` + `/metrics`, counters/logs move on a Discord message.
- Phase 2: `python -m evals.runner` against real Gemini produces the baseline report; router eval in plain pytest.
- Phase 3: bot end-to-end on podman Postgres; data-move rehearsal row counts match; CI green.
- Phase 4: live-marker cache+tools test; eval ≥ baseline; Discord smoke — "上週誰做最多家事?" gets a grounded answer.
- Phase 5: two test guilds — isolation + role gating verified in Discord; migrations idempotent.
- Each pillar ships with its docs page.
