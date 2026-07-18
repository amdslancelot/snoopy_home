import asyncio
import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional, Union

import aiosqlite
import discord
from discord import app_commands

from bot.client import bot
from config import settings
from core.context_manager import context_manager
from core.gemini_client import gemini_client
from core.llm_router import router
from core.message_parser import ComplexityAnalyzer
from core.observability import get_logger, metrics
from integrations.google_calendar import google_calendar
from integrations.voice_tts import speak_in_channel
from storage.models import HouseholdMember
from tasks.reminder import reminder_manager
from tasks.scheduler import init_scheduler, schedule_reminder, unschedule_reminder

log = get_logger("bot")

_analyzer = ComplexityAnalyzer()

# ── Voice retry state ─────────────────────────────────────────────────────────
# Keyed by reminder_id. Cleaned up when the user picks up or the loop exhausts.
_voice_pending: dict[int, dict] = {}
_VOICE_RETRY_INTERVAL = 60   # seconds between attempts
_VOICE_RETRY_MAX = 30        # give up after this many retries (30 minutes)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("ready", user=str(bot.user), user_id=bot.user.id)
    metrics.discord_events_total.labels(event="ready").inc()
    await _sync_household_context()
    await _restore_reminders()


@bot.event
async def on_member_join(member: discord.Member):
    metrics.discord_events_total.labels(event="member_join").inc()
    await _upsert_member(member)
    await _sync_household_context()


# ── Message handling ──────────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    metrics.discord_events_total.labels(event="message").inc()

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = (
        bot.user in message.mentions
        or any(r.name.lower() == settings.bot_name.lower() for r in message.role_mentions)
    )
    log.debug("message_received", author=str(message.author), is_dm=is_dm, is_mentioned=is_mentioned)

    if not (is_dm or is_mentioned):
        return

    text = re.sub(rf"<@[!&]?\d+>", "", message.content).strip()
    if not text:
        return

    async with message.channel.typing():
        await _handle(message, text)


async def _handle(message: discord.Message, text: str):
    channel_id = message.channel.id
    username = message.author.display_name

    await _upsert_member(message.author)

    complexity = _analyzer.analyze(text)
    model = router.select_model(complexity)
    metrics.router_tier_total.labels(tier=complexity.tier.name).inc()
    log.info("model_selected", tier=complexity.tier.name, model=model, summary=complexity.summary)

    context_manager.add_user(channel_id, text, username)

    try:
        reply_text, actions = await gemini_client.generate(
            context_manager.get(channel_id), model
        )
    except Exception as exc:
        log.error("generate_failed", error=str(exc))
        await message.channel.send("Sorry, I couldn't reach the AI right now. Please try again.")
        return

    for action in actions:
        kind = action.get("type") or "unknown"
        try:
            await _execute_action(action, message)
            metrics.action_executions_total.labels(action=kind, status="success").inc()
        except Exception:
            metrics.action_executions_total.labels(action=kind, status="error").inc()
            log.exception("action_failed", action=kind)

    if not reply_text and actions:
        reply_text = "Done!"
    elif not reply_text:
        log.warning("empty_reply", text=text[:80])
        reply_text = "Sorry, I didn't quite understand that. Could you rephrase?"

    context_manager.add_bot(channel_id, reply_text)
    await message.channel.send(reply_text)


# ── Action execution ──────────────────────────────────────────────────────────

async def _execute_action(action: dict, source: discord.Message):
    kind = action.get("type")
    if kind == "create_reminder":
        await _do_create_reminder(action, source)
    elif kind == "create_chore":
        await _do_create_chore(action, source)
    elif kind == "complete_chore":
        await _do_complete_chore(action, source)
    elif kind == "cancel_reminder":
        rid = action.get("reminder_id")
        if rid:
            await reminder_manager.mark_inactive(int(rid))
            unschedule_reminder(int(rid))
    elif kind == "update_profile":
        await _do_update_profile(action, source)
    elif kind == "create_calendar_event":
        await _do_create_calendar_event(action, source)
    elif kind == "delete_calendar_event":
        await _do_delete_calendar_event(action, source)
    elif kind == "update_calendar_event":
        await _do_update_calendar_event(action, source)
    elif kind == "speak_in_voice":
        await _do_speak_in_voice(action, source)


async def _do_create_reminder(action: dict, source: discord.Message):
    channel_id = source.channel.id
    creator_id = source.author.id

    target_id = await _resolve_user_id(
        action.get("target_user", ""), source.guild, creator_id
    )

    raw_dt = action.get("datetime") or ""
    is_recurring = bool(action.get("recurring"))
    cron_expr = action.get("cron")

    log.info(
        "reminder_action",
        target=action.get("target_user"),
        datetime=raw_dt,
        recurring=is_recurring,
        cron=cron_expr,
    )

    if is_recurring and cron_expr:
        trigger_time = datetime.utcnow()
    else:
        trigger_time = _parse_dt(raw_dt)

    now_utc = datetime.utcnow()

    if not trigger_time:
        await source.channel.send(
            f"Sorry {source.author.mention}, I couldn't parse that time. "
            "Can you be more specific? (e.g. 'tomorrow at 9 am')"
        )
        return

    if trigger_time <= now_utc:
        log.warning("reminder_trigger_in_past", trigger=str(trigger_time), raw=raw_dt)
        await source.channel.send(
            f"Sorry {source.author.mention}, I couldn't calculate that time correctly. "
            "Try again with an explicit time, e.g. 'remind me at 9:00 pm'."
        )
        return

    _MIN_LEAD = timedelta(seconds=10)
    if trigger_time - now_utc < _MIN_LEAD:
        trigger_time = now_utc + _MIN_LEAD
        log.info("reminder_trigger_adjusted", trigger=str(trigger_time))

    reminder = await reminder_manager.create(
        channel_id=channel_id,
        creator_id=creator_id,
        target_user_id=target_id,
        message=action.get("message", "Reminder"),
        trigger_time=trigger_time,
        is_recurring=is_recurring,
        cron_expression=cron_expr if is_recurring else None,
        voice=bool(action.get("voice", False)),
    )
    job_id = schedule_reminder(reminder)
    await reminder_manager.update_job_id(reminder.id, job_id)
    log.info(
        "reminder_created",
        reminder_id=reminder.id,
        target_user_id=target_id,
        trigger=str(trigger_time),
        job_id=job_id,
    )


async def _do_create_chore(action: dict, source: discord.Message):
    cron = action.get("cron")
    if not cron:
        return
    assigned_id = await _resolve_user_id(
        action.get("assigned_to", ""), source.guild, fallback_id=None
    )
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """INSERT INTO chore_tasks (channel_id, name, description, assigned_user_id, cron_expression)
               VALUES (?, ?, ?, ?, ?)""",
            (
                source.channel.id,
                action.get("name", "Unnamed chore"),
                action.get("description", ""),
                assigned_id,
                cron,
            ),
        )
        await db.commit()
    await _sync_household_context()


async def _do_update_profile(action: dict, source: discord.Message):
    target_id = await _resolve_user_id(
        action.get("target_user", ""), source.guild, source.author.id
    )
    updates = action.get("updates") or {}
    if not updates or not target_id:
        return
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT profile FROM household_members WHERE discord_id=?", (target_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        existing = json.loads(row["profile"] or "{}")
        existing.update(updates)
        await db.execute(
            "UPDATE household_members SET profile=? WHERE discord_id=?",
            (json.dumps(existing), target_id),
        )
        await db.commit()
    await _sync_household_context()


async def _do_complete_chore(action: dict, source: discord.Message):
    name = action.get("name", "").strip().lower()
    if not name:
        return
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "UPDATE chore_tasks SET last_completed=? WHERE lower(name)=? AND is_active=1",
            (datetime.utcnow().isoformat(), name),
        )
        await db.commit()


async def _do_create_calendar_event(action: dict, source: discord.Message):
    if not settings.household_calendar_id or not settings.google_service_account_json:
        await source.channel.send(
            "Google Calendar isn't configured yet. "
            "Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `HOUSEHOLD_CALENDAR_ID` in `.env`."
        )
        return

    raw_start = action.get("start_datetime") or ""
    raw_end = action.get("end_datetime")
    start = _parse_dt(raw_start)
    end = _parse_dt(raw_end) if raw_end else None

    if not start:
        await source.channel.send(
            f"Sorry {source.author.mention}, I couldn't parse the event start time."
        )
        return

    # Resolve attendee @usernames → google_email from their profiles
    attendee_emails: list[str] = []
    for mention in action.get("attendees") or []:
        username = mention.lstrip("@").lower()
        async with aiosqlite.connect(settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT profile FROM household_members WHERE lower(username)=? OR lower(display_name)=?",
                (username, username),
            ) as cur:
                row = await cur.fetchone()
        if row:
            profile = json.loads(row["profile"] or "{}")
            email = profile.get("google_email")
            if email:
                attendee_emails.append(email)

    ok = await google_calendar.create_event(
        title=action.get("title", "Untitled event"),
        description=action.get("description", ""),
        start=start,
        end=end,
        attendee_emails=attendee_emails,
    )
    if not ok:
        await source.channel.send(
            "Hmm, I couldn't create the calendar event. "
            "Check that the service account has edit access to the household calendar."
        )


async def _do_update_calendar_event(action: dict, source: discord.Message):
    if not settings.household_calendar_id or not settings.google_service_account_json:
        await source.channel.send(
            "Google Calendar isn't configured yet. "
            "Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `HOUSEHOLD_CALENDAR_ID` in `.env`."
        )
        return

    title = action.get("title", "").strip()
    start = _parse_dt(action.get("start_datetime")) if action.get("start_datetime") else None
    new_start = _parse_dt(action.get("new_start_datetime")) if action.get("new_start_datetime") else None
    new_end = _parse_dt(action.get("new_end_datetime")) if action.get("new_end_datetime") else None

    ok = await google_calendar.update_event(
        title=title,
        start=start,
        new_title=action.get("new_title") or None,
        new_start=new_start,
        new_end=new_end,
        new_description=action.get("new_description") if "new_description" in action else None,
    )
    if not ok:
        await source.channel.send(
            f"Hmm, I couldn't find \"{title}\" on the calendar"
            + (" around that time" if start else "")
            + " to update it. Check the event name or time?"
        )


async def _do_delete_calendar_event(action: dict, source: discord.Message):
    if not settings.household_calendar_id or not settings.google_service_account_json:
        await source.channel.send(
            "Google Calendar isn't configured yet. "
            "Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `HOUSEHOLD_CALENDAR_ID` in `.env`."
        )
        return

    title = action.get("title", "").strip()
    raw_start = action.get("start_datetime")
    start = _parse_dt(raw_start) if raw_start else None

    ok = await google_calendar.delete_event(title=title, start=start)
    if not ok:
        await source.channel.send(
            f"Hmm, I couldn't find \"{title}\" on the calendar"
            + (f" around that time" if start else "")
            + ". Double-check the event name or time?"
        )


async def _do_speak_in_voice(action: dict, source: discord.Message):
    text = action.get("message", "").strip()
    if not text:
        return

    target_id = await _resolve_user_id(
        action.get("target_user", ""), source.guild, source.author.id
    )
    await speak_in_channel(bot, source.guild, text, target_id)


# ── Slash commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="reminders", description="List your active reminders in this channel")
async def cmd_reminders(interaction: discord.Interaction):
    items = await reminder_manager.list_active(interaction.channel_id)
    if not items:
        await interaction.response.send_message("No active reminders.", ephemeral=True)
        return
    lines = [
        f"**#{r.id}** — {r.message} "
        f"({'recurring: ' + r.cron_expression if r.is_recurring else r.trigger_time.strftime('%Y-%m-%d %H:%M UTC')})"
        for r in items
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="show_chores", description="List active household chores")
async def cmd_show_chores(interaction: discord.Interaction):
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chore_tasks WHERE channel_id=? AND is_active=1 ORDER BY name",
            (interaction.channel_id,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("No chores scheduled.", ephemeral=True)
        return
    lines = []
    for r in rows:
        assignee = f" — <@{r['assigned_user_id']}>" if r["assigned_user_id"] else ""
        done = f" — last done {r['last_completed'][:10]}" if r["last_completed"] else ""
        lines.append(f"- **{r['name']}** [{r['cron_expression']}]{assignee}{done}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="chore", description="Add a recurring household chore")
@app_commands.describe(description="Chore and schedule, e.g. 'vacuum living room every Saturday at 11am'")
async def cmd_create_chore(interaction: discord.Interaction, description: str):
    await interaction.response.defer()

    messages = [{"role": "user", "content": f"Add a chore: {description}"}]
    complexity = _analyzer.analyze(description)
    model = router.select_model(complexity)

    try:
        reply_text, actions = await gemini_client.generate(messages, model)
    except Exception as exc:
        log.error("chore_cmd_generate_failed", error=str(exc))
        await interaction.followup.send("Sorry, I couldn't reach the AI right now.", ephemeral=True)
        return

    chore_action = next((a for a in actions if a.get("type") == "create_chore"), None)
    if not chore_action or not chore_action.get("cron"):
        await interaction.followup.send(
            reply_text or "Couldn't parse a schedule from that. Try: 'vacuum living room every Saturday at 11am'",
            ephemeral=True,
        )
        return

    assigned_id = await _resolve_user_id(
        chore_action.get("assigned_to", ""), interaction.guild, fallback_id=None
    )
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """INSERT INTO chore_tasks (channel_id, name, description, assigned_user_id, cron_expression)
               VALUES (?, ?, ?, ?, ?)""",
            (
                interaction.channel_id,
                chore_action.get("name", "Unnamed chore"),
                chore_action.get("description", ""),
                assigned_id,
                chore_action.get("cron"),
            ),
        )
        await db.commit()
    await _sync_household_context()
    await interaction.followup.send(reply_text or f"Added: {chore_action.get('name')}")


@bot.tree.command(name="todo", description="Add a to-do task for one or more members")
@app_commands.describe(
    title="What needs to be done",
    assigned_to="Who's responsible — name(s) separated by commas, or leave blank for unassigned",
)
async def cmd_create_todo(interaction: discord.Interaction, title: str, assigned_to: str = ""):
    user_ids = await _resolve_user_ids(assigned_to, interaction.guild)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "INSERT INTO todos (channel_id, title, assigned_user_ids) VALUES (?, ?, ?)",
            (interaction.channel_id, title.strip(), json.dumps(user_ids)),
        )
        await db.commit()
    mentions = " ".join(f"<@{uid}>" for uid in user_ids) if user_ids else ""
    suffix = f" — {mentions}" if mentions else ""
    await interaction.response.send_message(f"Added to-do: **{title}**{suffix}")


@bot.tree.command(name="summary", description="Show all to-dos and recurring chores")
async def cmd_summary(interaction: discord.Interaction):
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM todos WHERE channel_id=? AND is_active=1 ORDER BY created_at",
            (interaction.channel_id,),
        ) as cur:
            todos = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT * FROM chore_tasks WHERE channel_id=? AND is_active=1 ORDER BY name",
            (interaction.channel_id,),
        ) as cur:
            chores = [dict(r) for r in await cur.fetchall()]

    if not todos and not chores:
        await interaction.response.send_message("Nothing on the list yet!", ephemeral=True)
        return

    lines: list[str] = []

    if todos:
        lines.append("📋 **To-Do**")
        for t in todos:
            uids = json.loads(t["assigned_user_ids"] or "[]")
            mentions = " ".join(f"<@{uid}>" for uid in uids) if uids else "*(unassigned)*"
            lines.append(f"- {t['title']} — {mentions}")

    if chores:
        if lines:
            lines.append("")
        lines.append("🔄 **Recurring Chores**")
        for r in chores:
            assignee = f"<@{r['assigned_user_id']}>" if r["assigned_user_id"] else "*(unassigned)*"
            done = f" — last done {r['last_completed'][:10]}" if r["last_completed"] else ""
            lines.append(f"- **{r['name']}** [{r['cron_expression']}] — {assignee}{done}")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="remove_chore", description="Remove a recurring chore")
@app_commands.describe(name="Name of the chore to remove (partial name is fine)")
async def cmd_remove_chore(interaction: discord.Interaction, name: str):
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name FROM chore_tasks WHERE channel_id=? AND is_active=1",
            (interaction.channel_id,),
        ) as cur:
            rows = await cur.fetchall()

    match = _fuzzy_match(name, [(r["id"], r["name"]) for r in rows])
    if not match:
        options = ", ".join(f"**{r['name']}**" for r in rows) or "none"
        await interaction.response.send_message(
            f"Couldn't find a chore matching **{name}**. Active chores: {options}",
            ephemeral=True,
        )
        return

    rid, rname = match
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("UPDATE chore_tasks SET is_active=0 WHERE id=?", (rid,))
        await db.commit()
    await _sync_household_context()
    await interaction.response.send_message(f"Removed chore: **{rname}**")


@bot.tree.command(name="remove_todo", description="Remove a to-do task")
@app_commands.describe(title="Title of the to-do to remove (partial name is fine)")
async def cmd_remove_todo(interaction: discord.Interaction, title: str):
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title FROM todos WHERE channel_id=? AND is_active=1",
            (interaction.channel_id,),
        ) as cur:
            rows = await cur.fetchall()

    match = _fuzzy_match(title, [(r["id"], r["title"]) for r in rows])
    if not match:
        options = ", ".join(f"**{r['title']}**" for r in rows) or "none"
        await interaction.response.send_message(
            f"Couldn't find a to-do matching **{title}**. Active to-dos: {options}",
            ephemeral=True,
        )
        return

    rid, rtitle = match
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("UPDATE todos SET is_active=0 WHERE id=?", (rid,))
        await db.commit()
    await interaction.response.send_message(f"Removed to-do: **{rtitle}**")


@bot.tree.command(name="register", description="Register yourself as a household member")
async def cmd_register(interaction: discord.Interaction):
    await _upsert_member(interaction.user)
    await _sync_household_context()
    await interaction.response.send_message(
        f"Welcome, {interaction.user.display_name}! You're now registered as a household member.",
        ephemeral=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _resolve_user_id(
    mention_str: str,
    guild: Optional[discord.Guild],
    fallback_id: Optional[int],
) -> Optional[int]:
    if not mention_str or not guild:
        return fallback_id
    username = mention_str.lstrip("@").lower()
    if username in ("everyone", "here", "channel", "us", "group"):
        return 0  # sentinel: remind all household members
    member = discord.utils.find(
        lambda m: m.display_name.lower() == username or m.name.lower() == username,
        guild.members,
    )
    return member.id if member else fallback_id


def _fuzzy_match(query: str, candidates: list[tuple[int, str]]) -> Optional[tuple[int, str]]:
    """Return (id, name) of the best fuzzy match among candidates, or None if < 0.6 similarity."""
    if not candidates:
        return None
    q = query.lower()
    best, best_score = None, 0.0
    for cid, cname in candidates:
        c = cname.lower()
        if q == c:
            return (cid, cname)
        score = 0.9 if (q in c or c in q) else SequenceMatcher(None, q, c).ratio()
        if score > best_score:
            best_score, best = score, (cid, cname)
    return best if best_score >= 0.6 else None


async def _resolve_user_ids(names_str: str, guild: Optional[discord.Guild]) -> list[int]:
    if not names_str.strip() or not guild:
        return []
    ids: list[int] = []
    for name in names_str.split(","):
        uid = await _resolve_user_id(name.strip(), guild, fallback_id=None)
        if uid:
            ids.append(uid)
    return ids


async def _all_member_mentions() -> str:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT discord_id FROM household_members WHERE is_active=1"
        ) as cur:
            rows = await cur.fetchall()
    parts = []
    for r in rows:
        user = bot.get_user(r["discord_id"])
        parts.append(user.mention if user else f"<@{r['discord_id']}>")
    return " ".join(parts) if parts else "@here"


def _parse_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return reminder_manager.parse_datetime(raw)


async def _upsert_member(user: Union[discord.Member, discord.User]):
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """INSERT INTO household_members (discord_id, username, display_name)
               VALUES (?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
                 username=excluded.username,
                 display_name=excluded.display_name""",
            (user.id, user.name, getattr(user, "display_name", user.name)),
        )
        await db.commit()


async def _sync_household_context():
    """Refresh gemini_client's cached household section from the database."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT username, display_name, profile FROM household_members WHERE is_active=1"
        ) as cur:
            members = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT name, description, cron_expression, assigned_user_id FROM chore_tasks WHERE is_active=1"
        ) as cur:
            chores_raw = [dict(r) for r in await cur.fetchall()]

    # Enrich chores with assigned username
    chores = []
    for c in chores_raw:
        entry = dict(c)
        if c["assigned_user_id"]:
            match = next((m for m in members if True), None)
            async with aiosqlite.connect(settings.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT username FROM household_members WHERE discord_id=?",
                    (c["assigned_user_id"],),
                ) as cur:
                    row = await cur.fetchone()
                    entry["assigned_username"] = row["username"] if row else None
        else:
            entry["assigned_username"] = None
        chores.append(entry)

    gemini_client.update_household(members, chores)


async def _restore_reminders():
    """On startup reschedule all active reminders and wire the fire callback."""
    init_scheduler(_fire_reminder)
    all_reminders = await reminder_manager.get_all_active()
    restored = 0
    for r in all_reminders:
        if r.is_recurring or r.trigger_time > datetime.utcnow():
            schedule_reminder(r)
            restored += 1
    log.info("reminders_restored", count=restored)


async def _fire_reminder(
    channel_id: int,
    target_user_id: int,
    message: str,
    reminder_id: int,
    is_recurring: bool,
    voice: bool = False,
):
    metrics.reminders_fired_total.inc()
    log.info(
        "reminder_firing",
        reminder_id=reminder_id,
        channel_id=channel_id,
        target=target_user_id,
        voice=voice,
    )
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:
            log.error("reminder_channel_fetch_failed", channel_id=channel_id, error=str(exc))
            return

    guild = getattr(channel, "guild", None)

    if target_user_id == 0:
        # Group reminder — ping all registered household members
        mention = await _all_member_mentions()
        await channel.send(f"⏰ {mention} — {message}")
        if voice and guild:
            await speak_in_channel(bot, guild, message, None)
    else:
        # Single-user reminder
        user = bot.get_user(target_user_id) if target_user_id else None
        mention = user.mention if user else (f"<@{target_user_id}>" if target_user_id else "")
        member = guild.get_member(target_user_id) if (guild and target_user_id) else None
        user_in_voice = bool(member and member.voice and member.voice.channel)

        if voice and not user_in_voice and settings.default_voice_channel_id:
            voice_link = f"<#{settings.default_voice_channel_id}>"
            await channel.send(f"⏰ {mention} — {message}\nJoin voice to hear it 👉 {voice_link}")
        else:
            await channel.send(f"⏰ {mention} — {message}")

        if voice and guild:
            if user_in_voice:
                await speak_in_channel(bot, guild, message, target_user_id)
            else:
                _start_voice_retry(reminder_id, channel_id, guild.id, target_user_id, message)

    if not is_recurring:
        await reminder_manager.mark_inactive(reminder_id)


def _start_voice_retry(
    reminder_id: int,
    channel_id: int,
    guild_id: int,
    target_user_id: int,
    message: str,
):
    existing = _voice_pending.get(reminder_id)
    if existing:
        existing["task"].cancel()

    task = asyncio.create_task(
        _voice_retry_loop(reminder_id, channel_id, guild_id, target_user_id, message)
    )
    _voice_pending[reminder_id] = {
        "task": task,
        "target_user_id": target_user_id,
        "guild_id": guild_id,
        "message": message,
        "channel_id": channel_id,
    }
    log.info("voice_retry_started", reminder_id=reminder_id, target=target_user_id)


async def _voice_retry_loop(
    reminder_id: int,
    channel_id: int,
    guild_id: int,
    target_user_id: int,
    message: str,
):
    for attempt in range(1, _VOICE_RETRY_MAX + 1):
        try:
            await asyncio.sleep(_VOICE_RETRY_INTERVAL)
        except asyncio.CancelledError:
            # on_voice_state_update already handled playback
            log.info("voice_retry_cancelled", reminder_id=reminder_id)
            return

        if reminder_id not in _voice_pending:
            return  # Handled concurrently by on_voice_state_update

        guild = bot.get_guild(guild_id)
        member = guild.get_member(target_user_id) if guild else None

        if member and member.voice and member.voice.channel:
            log.info("voice_retry_playing", attempt=attempt, target=target_user_id)
            _voice_pending.pop(reminder_id, None)
            await speak_in_channel(bot, guild, message, target_user_id)
            return

        log.debug("voice_retry_waiting", attempt=attempt, max=_VOICE_RETRY_MAX, target=target_user_id)

    # All 30 attempts exhausted — send text fallback
    _voice_pending.pop(reminder_id, None)
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send(
            f"⏰ {_mention(target_user_id)} — couldn't reach you in voice after 30 minutes: {message}"
        )
    log.info("voice_retry_exhausted", reminder_id=reminder_id)


def _mention(user_id: Optional[int]) -> str:
    if not user_id:
        return ""
    user = bot.get_user(user_id)
    return user.mention if user else f"<@{user_id}>"


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    # Only care when a user joins or moves to a voice channel
    if not (after.channel and after.channel != before.channel):
        return

    for rid in [rid for rid, info in _voice_pending.items() if info["target_user_id"] == member.id]:
        info = _voice_pending.pop(rid, None)
        if info is None:
            continue
        info["task"].cancel()
        log.info("voice_pickup", member_id=member.id, reminder_id=rid)
        await speak_in_channel(bot, member.guild, info["message"], member.id)
