# Best Prompt To Code The Entire Project

Build a Discord AI home assistant bot in Python 3.11 called "Snoopy" for a household
group server (couple + bot). Stack: discord.py 2.x, google-genai SDK (Gemini),
aiosqlite (SQLite), APScheduler 3.x AsyncIOScheduler, pydantic-settings, dateparser.

Architecture:
- bot/client.py: discord.Client subclass with slash command tree
- bot/events.py: on_message handler (triggers on @mention or DM)
- core/message_parser.py: ComplexityAnalyzer scoring messages 0–12 across 6
  rule-based dimensions (token_estimate, reasoning_depth, multi_step,
  temporal_complexity, context_dependency, domain_complexity) → LOW/MEDIUM/HIGH tier
- core/llm_router.py: maps tier → model name (gemini-2.5-flash-lite /
  gemini-2.5-flash / gemini-2.5-pro)
- core/gemini_client.py: Gemini API client with server-side context caching
  (one cache per model; system prompt + household roster + chore schedule cached;
  current date stamped dynamically on each request, NOT in the cache; falls back
  to uncached on failure)
- core/context_manager.py: per-channel sliding window conversation history
- tasks/reminder.py + tasks/scheduler.py: reminder CRUD in SQLite, APScheduler
  scheduling with fire callback injected at startup to avoid circular imports
- storage/database.py + storage/models.py: SQLite schema (reminders, chore_tasks,
  household_members)

Key design decisions:
1. LLM uses <action>{JSON}</action> blocks at en
   operations (create_reminder, create_chore, complete_chore, cancel_reminder);
   bot strips these before displaying to users
2. System prompt must exceed 4096 tokens (Gemini paid-tier cache minimum); pad
   with home management knowledge base (chore fr
   appliance schedules)
3. bot.events imported inside main() not at moduer
   after event loop starts
4. Requires Discord Privileged Intents: Server M
5. DISCORD_GUILD_ID env var accepts empty string (treat as None)
6. Slash commands: /register, /reminders, /chore
