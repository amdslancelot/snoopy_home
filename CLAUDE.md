# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
python main.py
```

Requires a `.env` file — copy `.env.example` and fill in `DISCORD_TOKEN` and `GEMINI_API_KEY`. The bot uses Python 3.11+.

To list available Gemini models for the current API key:
```bash
python3 -c "
from google import genai
from config import settings
client = genai.Client(api_key=settings.gemini_api_key)
for m in client.models.list():
    print(m.name)
"
```

## Architecture

### Request flow

```
Discord mention/@DM
  → bot/events.py on_message
  → ComplexityAnalyzer.analyze(text)      # scores 0–12, picks model tier
  → LLMRouter.select_model(result)        # tier → Gemini model name
  → GeminiClient.generate(messages, model)
      ├── _ensure_cache(model)            # create/reuse server-side prompt cache
      ├── _stamp_date(messages)           # prepend UTC time to last user message
      └── models.generate_content(...)
  → _extract_actions(response)            # strip <action>{JSON}</action> blocks
  → _execute_action(action, message)      # create reminder / chore / mark done
  → channel.send(display_text)
```

### Complexity routing (`core/message_parser.py`)

`ComplexityAnalyzer` scores messages across 6 rule-based dimensions (total 0–12) and maps to a model tier without an extra LLM call:

| Score | Tier | Model |
|---|---|---|
| 0–3 | LOW | `gemini-2.5-flash-lite` |
| 4–7 | MEDIUM | `gemini-2.5-flash` |
| 8–12 | HIGH | `gemini-2.5-pro` |

Thresholds are set in `config.py` (`complexity_medium_threshold`, `complexity_high_threshold`). Models are configured via `MODEL_LOW/MEDIUM/HIGH` env vars.

### Prompt caching (`core/gemini_client.py`)

The static system prompt + household roster + chore schedule are cached server-side on Gemini (one `_CacheHandle` per model). The cache is invalidated when `update_household()` detects a change. Current date/time is **not** in the cache — it is prepended to the last user message in `_stamp_date()` so the cache never goes stale over time.

Minimum token requirement: **4096 tokens** (Gemini API constraint, paid tier only). Cache creation failures fall back to uncached generation transparently.

### Action protocol

The LLM embeds structured JSON at the end of replies:
```
<action>{"type": "create_reminder", ...}</action>
```
`_extract_actions()` strips these before displaying to users and returns them as dicts. Supported types: `create_reminder`, `create_chore`, `complete_chore`, `cancel_reminder`. The action schema is defined in the system prompt in `_SYSTEM_PROMPT_STATIC`.

### Reminder scheduling (`tasks/scheduler.py`, `tasks/reminder.py`)

APScheduler `AsyncIOScheduler` with MemoryJobStore. Job IDs are `reminder_{id}`. The fire callback is injected at startup via `init_scheduler()` (in `bot/events.py _restore_reminders`) to avoid a circular import. All active reminders are rescheduled from SQLite on `on_ready`.

### Storage

SQLite via `aiosqlite`. Schema is initialised in `storage/database.py:init_db()` called from `main.py`. Three tables: `reminders`, `chore_tasks`, `household_members`.

## Key constraints

- `bot/events.py` imports from `core/`, `tasks/`, `storage/` — never the reverse.
- `tasks/scheduler.py` has no import from `bot/` — the fire callback is injected.
- `bot.events` is imported inside `main()` (not at module level) so decorators register after the event loop starts.
- Discord bot requires two **Privileged Gateway Intents** enabled in the Developer Portal: **Server Members Intent** and **Message Content Intent**.
- Context caching requires a **paid** Gemini API tier. The free tier returns `limit: 0`.
