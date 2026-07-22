# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Postgres first (data lives here, not in SQLite anymore). Dev and staging
# share one Postgres instance, hosted in minikube with etcd encryption-at-rest
# — see docs/prod-provisioning.md:
./deploy/setup-minikube.sh   # fresh cluster / after `minikube delete`; idempotent otherwise
kubectl -n data port-forward svc/postgres 5432:5432 &   # keep running (shared Postgres lives in the `data` namespace)
python main.py           # runs migrations, starts health server + bot
```

Don't run this alongside the staging bot pod at the same time — both would
schedule reminders and write chore/todo state against the same rows.

Requires a `.env` — copy `.env.example`; minimum `DISCORD_TOKEN`, `GEMINI_API_KEY`, `DATABASE_URL` (no code default — see `.env.example`). Python 3.11+.

Tests: `pytest tests/` (unit + Postgres integration; integration skips without a reachable `TEST_DATABASE_URL` database — create `snoopy_test` on the local Postgres; `live`-marked tests hit the real Gemini API and only run with a real key). Evals: `python -m evals.runner [--judge]`.

## Architecture

### Request flow

```
Discord mention/DM
  → bot/events.py on_message
  → guild/home-guild resolution + admin check     # docs/multi-tenancy.md
  → ComplexityAnalyzer.analyze(text)              # 0–12 score → tier
  → LLMRouter.select_model(result)                # tier → Gemini model
  → GeminiClient.generate_with_tools(msgs, model, ctx, household=...)
      ├── _ensure_cache(model)     # static prompt + tool schemas, server-side
      ├── household block          # per-guild roster/chores, per-request message
      └── tool loop (cap 5)        # function calls → registry executors → results fed back
  → channel.send(reply)
```

The LLM acts through **Gemini native function calling** (15 tools: 9 writes + 6 reads, `core/tools/`). Write executors are the handlers in `bot/events.py`, registered into the registry at import time; read executors live in `core/tools/read_tools.py`. `ACTION_PROTOCOL=legacy` restores the old `<action>{JSON}</action>` regex protocol for one release. See `docs/function-calling.md`.

### Complexity routing (`core/message_parser.py`)

Six rule-based dimensions (0–12) → LOW `gemini-2.5-flash-lite` / MEDIUM `gemini-2.5-flash` / HIGH `gemini-2.5-pro` (`MODEL_LOW/MEDIUM/HIGH` env vars). No extra LLM call. Router behavior is regression-pinned by the golden dataset (`tests/unit/test_router_eval.py`).

### Prompt caching (`core/gemini_client.py`)

The static system prompt + tool declarations are cached server-side per model (4096-token minimum, paid tier only; creation failure falls back transparently). The cache is fully static and shared across guilds: household data rides as a per-request context message (`core/household.py`), and the current datetime is stamped onto the last user message (`_stamp_date`). A request using `cached_content` must NOT also pass `tools`/`system_instruction` — the uncached fallback passes both inline.

### Storage — PostgreSQL (`storage/`, `docs/storage.md`)

Plain asyncpg (no ORM), pool max 5. ALL SQL lives in `storage/repositories.py`; numbered `.sql` migrations in `storage/migrations/` run at startup via `storage/migrate.py`. Datetimes: `timestamptz` in DB, naive UTC in Python (converted at the repo boundary). JSONB ↔ dict via codecs. Every query on shared tables carries `guild_id` (multi-household isolation, `docs/multi-tenancy.md`).

### Evals (`evals/`, `docs/evals.md`) — run before shipping prompt/tool changes

55-case golden dataset driving the real pipeline; deterministic scorers gate at ≥90% pass rate, LLM-judge is report-only. Router eval runs free in normal pytest. CI: nightly + `eval` PR label (`.github/workflows/eval.yml`).

### Observability (`core/observability.py`, `docs/observability.md`)

structlog everywhere (never `print()`); Prometheus metrics + `/health` + `/ready` on port 8080 (`web/health.py`); Grafana dashboard in `deploy/grafana/`.

### Reminder scheduling (`tasks/scheduler.py`)

APScheduler `AsyncIOScheduler`, MemoryJobStore, job IDs `reminder_{id}`; fire callback injected at startup (`init_scheduler`) to avoid a circular import; active reminders rescheduled from Postgres on `on_ready`.

## Key constraints

- `bot/events.py` imports from `core/`, `tasks/`, `storage/` — never the reverse. Injection patterns (`init_scheduler`, tool-executor registration) keep it that way.
- `tasks/scheduler.py` has no import from `bot/`.
- `bot.events` is imported inside `main()` (not at module level) so decorators register after the event loop starts.
- Discord bot needs two **Privileged Gateway Intents**: Server Members + Message Content.
- Context caching requires a **paid** Gemini API tier (free tier returns `limit: 0`).
- Admin = Discord Manage Server/Administrator, computed live — never stored.
- Deploy: prod on single-node k3s (fresh OCI node) — cutover runbook `docs/prod-k3s-runbook.md`, design rationale `deploy/PLAN-DEPLOY-K3S.md`, manifests `deploy/k8s/`; staging on minikube (`docs/prod-provisioning.md`). The upgrade history/plan is `docs/UPGRADE-PLAN.md`.
