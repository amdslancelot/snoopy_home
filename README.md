# Snoopy Home

A Discord AI home assistant for a household group chat. Add it to a shared server with your housemates and manage reminders, chores, household communication, Google Calendar events, and voice announcements through natural conversation.

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A reachable **PostgreSQL 17** instance and a `DATABASE_URL` — required, no code default (`config.py`). See [`docs/storage.md`](docs/storage.md) for schema/migration details and [`docs/dev-stage-minikube-runbook.md`](docs/dev-stage-minikube-runbook.md) for how to actually stand one up locally (a minikube-hosted Postgres reached via `kubectl port-forward`).
- `ffmpeg` installed and on your `PATH` (required for voice TTS playback)
- A [Discord bot token](https://discord.com/developers/applications) with **Server Members Intent** and **Message Content Intent** enabled
- A [Google Gemini API key](https://aistudio.google.com/) (paid tier required for prompt caching)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Fill in DISCORD_TOKEN, GEMINI_API_KEY, and DATABASE_URL at minimum
```

### 4. Run

```bash
python main.py   # applies pending storage/migrations/*.sql, then starts the bot
```

---

## Optional integrations

### DISCORD_GUILD_ID

Makes slash commands (`/reminders`, `/chores`, `/register`) register instantly on your server instead of taking up to an hour to propagate globally.

1. In Discord: **Settings → Advanced → enable Developer Mode**
2. Right-click your server name in the left sidebar → **Copy Server ID**
3. Add to `.env`:
   ```
   DISCORD_GUILD_ID=123456789012345678
   ```

### Google Calendar

Snoopy can create events on a shared household Google Calendar.

**Step 1 — Google Cloud project**
1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a new project
2. Search for **Google Calendar API** → Enable it

**Step 2 — Service account**
1. In the left menu: **IAM & Admin → Service Accounts**
2. Click **Create Service Account** → give it a name (e.g. `snoopy-bot`) → Done
3. Click the service account → **Keys tab → Add Key → Create new key → JSON**
4. A `.json` file downloads — save it in your project folder

**Step 3 — Share your calendar with the service account**
1. Open [calendar.google.com](https://calendar.google.com)
2. Find your household calendar → three dots → **Settings and sharing**
3. Under **Share with specific people** → add the service account email (open the downloaded JSON and look for `client_email`, e.g. `snoopy-bot@your-project.iam.gserviceaccount.com`) → set permission to **Make changes to events**
4. Still in calendar settings → scroll to **Integrate calendar** → copy the **Calendar ID** (e.g. `abc123@group.calendar.google.com`)

**Step 4 — `.env`**
```
GOOGLE_SERVICE_ACCOUNT_JSON=your-key-file.json
HOUSEHOLD_CALENDAR_ID=abc123@group.calendar.google.com
```

The filename can be anything — use whatever you saved the JSON file as.

**Step 5 — Member email invites (optional)**
For members to receive Google Calendar invites, tell Snoopy their email:
```
@Snoopy my Google email is alice@gmail.com
```

### Discord voice TTS

Snoopy can join a voice channel and read reminders or announcements aloud using text-to-speech (`edge-tts` — free, no API key).

**Step 1 — Install ffmpeg**
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Verify
ffmpeg -version
```

**Step 2 — Install Python dependencies**
```bash
pip install edge-tts PyNaCl
```

**Step 3 — Get your voice channel ID (optional fallback)**

This is the channel Snoopy joins when the target user isn't already in voice. When a voice reminder fires and the member isn't in voice, the text ping will include a one-tap link to this channel.

1. In Discord: **Settings → Advanced → enable Developer Mode** (if not already on)
2. Right-click any voice channel (speaker icon 🔊) in the left sidebar → **Copy Channel ID**
   - If you don't have a voice channel yet: right-click your server → **Create Channel → Voice Channel**
3. Add to `.env`:
   ```
   DEFAULT_VOICE_CHANNEL_ID=123456789012345678
   ```

This setting is optional — if omitted, Snoopy only speaks when the target user is already in a voice channel.

**Step 4 — Use it**

Add "vocally" or "out loud" to trigger voice on a reminder:
```
@Snoopy remind me vocally at 9 pm to take my meds
@Snoopy remind my wife vocally to start cooking in 1 min
@Snoopy announce to the house that dinner is ready
```

When the reminder fires and the member isn't in voice, the ping includes a tap-to-join link:
```
⏰ @Tiff — Time to start cooking!
Join voice to hear it 👉 #general
```

Snoopy speaks the moment they join. If they don't join within 30 minutes, a final text message is sent.

---

## Usage

Mention the bot or DM it directly. No slash commands needed for most things — just talk naturally.

### Reminders

```
@Snoopy remind me to take medication at 8 pm
@Snoopy remind my wife to leave in 10 minutes
@Snoopy every Monday at 9 am remind us to take out the bins
@Snoopy remind me vocally at 9 pm to take my medication
@Snoopy cancel my plants reminder
```

### Chores

```
@Snoopy add a chore — vacuum the living room every Saturday at 11 am
@Snoopy assign dishwasher duty to Alex every weekday evening
@Snoopy I just finished vacuuming
@Snoopy what chores are due this week?
```

### Google Calendar

```
@Snoopy add a calendar event — dentist Thursday at 10 am
@Snoopy put a dinner party on Saturday 7 pm and invite Alice and Bob
```

### Voice announcements

```
@Snoopy announce to the house that dinner is ready
@Snoopy say aloud: everyone come to the kitchen
```

### Member profiles

Snoopy learns about you as you chat — no explicit command needed:

```
@Snoopy by the way I'm 32 and I usually wake up at 7 am
@Snoopy I'm lactose intolerant
@Snoopy my Google email is alice@gmail.com
@Snoopy what do you know about me?
```

### Slash commands

| Command | Description |
|---|---|
| `/reminders` | List your active reminders in this channel |
| `/summary` | Show all to-dos and recurring chores with assigned members |
| `/show_chores` | List recurring chores with assigned members |
| `/chore <description>` | Add a recurring chore, e.g. `/chore vacuum living room every Saturday at 11am` |
| `/remove_chore <name>` | Remove a recurring chore (partial name match) |
| `/todo <title> [assigned_to]` | Add a one-off to-do task, optionally assign to member(s) |
| `/remove_todo <title>` | Remove a to-do task (partial name match) |
| `/register` | Manually register as a household member (auto-happens on first message) |

---

## How it works

### Message flow

```
Discord mention / DM
  │
  ▼
on_message (bot/events.py)
  │  Strip bot mention, skip empty text
  │
  ▼
ComplexityAnalyzer.analyze(text)         ← rule-based, no LLM call
  │  Scores message 0–12 across 6 dimensions:
  │    token_estimate, reasoning_depth, multi_step,
  │    temporal_complexity, context_dependency, domain_complexity
  │
  ▼
LLMRouter.select_model(score)            ← maps score → model name
  │  0–3  → gemini-2.5-flash-lite  (LOW)
  │  4–7  → gemini-2.5-flash       (MEDIUM)
  │  8–12 → gemini-2.5-pro         (HIGH)
  │
  ▼
format_household(members, chores)        ← per-guild roster + chores, built fresh
  │                                          from Postgres every request (core/household.py) —
  │                                          NOT part of the cached prompt
  ▼
GeminiClient.generate_with_tools(messages, model, ctx, household)
  │  Native function-calling loop (default; ACTION_PROTOCOL=tools):
  │  ├─ _ensure_cache(model)     create/reuse server-side cache of the STATIC
  │  │                           prefix only — persona + rules + tool schemas
  │  │                           (once/hour); household rides in uncached
  │  ├─ _stamp_date(messages)    prepend current UTC + household-local time
  │  ├─ models.generate_content(...) → model replies with text, or with one
  │  │   or more function calls
  │  └─ if function calls: execute each via ToolRegistry, feed the results
  │      back as function_response parts, and repeat (capped at 5 rounds)
  │      until the model returns plain text with no further calls
  │
  ▼
channel.send(display_text)
```

Tool executors (`create_reminder`, `create_chore`, `complete_chore`,
`cancel_reminder`, `update_profile`, `create_calendar_event`,
`speak_in_voice`, plus 6 read-only tools such as `list_reminders` and
`chore_stats`) live in `bot/events.py` (writes) and `core/tools/read_tools.py`
(reads), and persist through `storage/repositories.py` against Postgres.

A legacy `<action>{"type": ...}</action>` regex protocol (`GeminiClient.generate`
+ `_extract_actions`) still exists behind `ACTION_PROTOCOL=legacy` as a
rollback switch — see [`docs/function-calling.md`](docs/function-calling.md) —
but native function calling is the default and the actively developed path.

### LLM calls per message

The complexity router is entirely rule-based, so there is no "routing LLM
call." Under the default tools protocol, `generate_with_tools` makes exactly
one `generate_content` call when the model answers directly, or one call per
round-trip when it invokes tools (call → execute → feed result back → next
call), up to `max_iterations` (5) before giving up. The legacy protocol
always makes exactly one call per message.

### Prompt caching

Only the **static prefix** — persona, capability rules, and the tool/action
schemas — is cached server-side on Gemini. It never varies per guild, so one
cache is shared across every household.

The **household roster and chore schedule are NOT cached**: they're rebuilt
from Postgres and injected as a small leading context message on every
request (`core/household.py`), since they differ per guild and change often
— caching them per-guild would multiply the 4096-token cache floor by the
number of guilds.

- One cache per model tier (LOW / MEDIUM / HIGH), shared across all guilds
- Cache TTL: 1 hour (configurable via `CACHE_TTL_SECONDS`)
- Current date/time is also **not** cached — it is prepended to each message at request time
- If cache creation fails (e.g. below the token floor), generation falls back to uncached mode transparently

Minimum 4096 tokens required for caching (Gemini paid tier constraint). The static prefix is sized to exceed this comfortably.

### Member profiles

Profiles are stored as a `JSONB` column per member in Postgres
(`household_members.profile`). Snoopy accumulates them passively — whenever a
member mentions a personal fact in conversation, the model calls
`update_profile`, which merges the new data into the existing profile
server-side (`profile || $1::jsonb`, no read-modify-write race). The full
profile is included in the household context injected into every request.

```json
{
  "age": 32,
  "wake_time": "7:00 AM",
  "diet": "lactose intolerant",
  "medications": "vitamin D daily",
  "google_email": "alice@gmail.com"
}
```

The `google_email` key is used by the calendar integration to send event invites.

### Reminder scheduling

APScheduler `AsyncIOScheduler` (timezone: UTC) with MemoryJobStore. All active reminders are rescheduled from Postgres on startup. One-time reminders use `DateTrigger` with an explicit UTC datetime; recurring reminders use `CronTrigger` with UTC timezone.

Reminders with `voice: true` play TTS audio in the target user's voice channel (or the `DEFAULT_VOICE_CHANNEL_ID` fallback) in addition to the normal text ping.

### Google Calendar integration

`integrations/google_calendar.py` wraps the Google Calendar API v3 with a service account. The synchronous API call runs in an executor to avoid blocking the event loop. If `GOOGLE_SERVICE_ACCOUNT_JSON` or `HOUSEHOLD_CALENDAR_ID` is not set, the action handler sends a configuration hint in Discord instead of failing silently.

### Voice TTS integration

`integrations/voice_tts.py` uses `edge-tts` (Microsoft Edge TTS — free, async, no API key) to synthesise speech to a temporary MP3 file, then plays it via `discord.FFmpegPCMAudio`. The bot connects to the target user's current voice channel, waits for playback to finish, disconnects, and deletes the temp file.

---

## Architecture

```
snoopy_home/
├── main.py                  Entry point — run migrations, init pool, start bot
├── config.py                Settings via pydantic-settings + .env (DATABASE_URL required)
├── bot/
│   ├── client.py            HomeBot subclass (intents, tree)
│   └── events.py            on_message, slash commands, write-tool/action handlers
├── core/
│   ├── gemini_client.py     Gemini API client: prompt caching, native tool-calling loop,
│   │                        legacy action extraction
│   ├── llm_router.py        Complexity score → model name
│   ├── message_parser.py    ComplexityAnalyzer (rule-based, 6 dimensions)
│   ├── context_manager.py   Per-channel sliding window of conversation history
│   ├── household.py         Per-guild roster + chore block, injected uncached per request
│   ├── observability.py     Structured logging (structlog) + Prometheus metrics
│   └── tools/
│       ├── declarations.py  FunctionDeclarations for all 15 tools (9 writes + 6 reads)
│       ├── registry.py      ToolSpec/ToolRegistry/ToolContext for native function calling
│       └── read_tools.py    Read-only tool executors (repositories + calendar)
├── integrations/
│   ├── google_calendar.py   Service-account Google Calendar client
│   └── voice_tts.py         edge-tts synthesis + Discord voice channel playback
├── tasks/
│   ├── scheduler.py         APScheduler wrapper, UTC DateTrigger/CronTrigger
│   └── reminder.py          Back-compat re-exports of storage/repositories.py's ReminderRepository
├── storage/
│   ├── migrate.py           Versioned migration runner (storage/migrations/*.sql)
│   ├── migrations/          Numbered .sql files, tracked in schema_migrations
│   ├── pool.py              asyncpg connection pool + JSONB↔dict codecs
│   ├── repositories.py      All SQL: Reminder/Chore/Todo/Member repositories
│   └── models.py            Reminder, ChoreTask, HouseholdMember dataclasses (row types)
├── scripts/
│   └── migrate_sqlite_to_pg.py   One-time legacy SQLite → Postgres data move
└── web/
    └── health.py             /health endpoint (bot ready + DB ping + scheduler running)
```

See [`docs/storage.md`](docs/storage.md) for the storage layer in depth and
[`docs/function-calling.md`](docs/function-calling.md) for the tool-calling
loop and action-protocol rollback switch.

### Key constraints

- `bot/events.py` imports from `core/`, `tasks/`, `storage/`, `integrations/` — never the reverse
- `tasks/scheduler.py` has no import from `bot/` — the fire callback is injected at startup
- `bot.events` is imported inside `main()` so decorators register after the event loop starts
- Datetimes are `TIMESTAMPTZ` in Postgres; naive UTC in Python everywhere above `storage/` — the repository layer converts at the boundary
- Google Calendar and voice TTS are opt-in — the bot starts and runs normally without their env vars set
- `DATABASE_URL` has no code default — every environment (dev/staging/prod) must set it explicitly
