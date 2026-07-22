import asyncio
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional, Union

import discord
from discord import app_commands

from bot.client import bot
from config import settings
from core.context_manager import context_manager
from core.gemini_client import gemini_client
from core.llm_router import router
from core.message_parser import ComplexityAnalyzer
from core.household import PREAMBLE, format_household
from core.observability import get_logger, metrics
from core.tools import read_tools
from core.tools.declarations import DECLARATIONS
from core.tools.registry import ToolContext, registry
from integrations.google_calendar import google_calendar
from integrations.voice_tts import speak_in_channel
from storage.repositories import chore_repo, member_repo, todo_repo, user_settings_repo
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
    await _restore_reminders()


@bot.event
async def on_member_join(member: discord.Member):
    metrics.discord_events_total.labels(event="member_join").inc()
    await _upsert_member(member, member.guild.id)


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

    # Household scope: the guild the message came from, or the author's home
    # guild for DMs (auto-adopted when they share exactly one server).
    if message.guild:
        guild = message.guild
        guild_id = guild.id
    else:
        guild_id = await _resolve_home_guild(message.author)
        if guild_id is None:
            await message.channel.send(
                "I don't know which household you belong to yet — run `/set_home` "
                "in your household server once, then DM me again."
            )
            return
        guild = bot.get_guild(guild_id)

    is_admin = _member_is_admin(guild, message.author.id)
    await _upsert_member(message.author, guild_id)
    household = PREAMBLE + format_household(
        await member_repo.active_members(guild_id),
        await chore_repo.list_all_active(guild_id),
    )

    complexity = _analyzer.analyze(text)
    model = router.select_model(complexity)
    metrics.router_tier_total.labels(tier=complexity.tier.name).inc()
    log.info("model_selected", tier=complexity.tier.name, model=model, summary=complexity.summary)

    context_manager.add_user(channel_id, text, username)
    log.debug("user_message", channel_id=channel_id, author=username, text=text)

    try:
        if settings.action_protocol == "tools":
            ctx = ToolContext(
                channel_id=channel_id,
                author_id=message.author.id,
                author_name=username,
                guild=guild,
                guild_id=guild_id,
                is_admin=is_admin,
                source_message=message,
            )
            # Tool executors run inside the loop (metrics recorded there).
            reply_text, actions = await gemini_client.generate_with_tools(
                context_manager.get(channel_id), model, ctx, household=household
            )
        else:
            reply_text, actions = await gemini_client.generate(
                context_manager.get(channel_id), model, household=household
            )
            for action in actions:
                kind = action.get("type") or "unknown"
                try:
                    await _execute_action(action, message, guild_id, is_admin)
                    metrics.action_executions_total.labels(action=kind, status="success").inc()
                except Exception:
                    metrics.action_executions_total.labels(action=kind, status="error").inc()
                    log.exception("action_failed", action=kind)
    except Exception as exc:
        log.error("generate_failed", error=str(exc))
        await message.channel.send("Sorry, I couldn't reach the AI right now. Please try again.")
        return

    if not reply_text and actions:
        reply_text = "Done!"
    elif not reply_text:
        log.warning("empty_reply", text=text[:80])
        reply_text = "Sorry, I didn't quite understand that. Could you rephrase?"

    context_manager.add_bot(channel_id, reply_text)
    log.debug("bot_reply", channel_id=channel_id, text=reply_text, actions=actions)
    await message.channel.send(reply_text)


# ── Action execution ──────────────────────────────────────────────────────────

_ACTION_HANDLERS = {}  # populated below, after the handlers are defined


async def _execute_action(action: dict, source: discord.Message, guild_id: int = 0, is_admin: bool = False):
    handler = _ACTION_HANDLERS.get(action.get("type"))
    if handler:
        await handler(action, source, guild_id=guild_id, is_admin=is_admin)


async def _do_cancel_reminder(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    rid = action.get("reminder_id")
    if not rid:
        return {"ok": False, "error": "missing reminder_id"}
    reminder = await reminder_manager.get(int(rid))
    if reminder is None or not reminder.is_active:
        return {"ok": False, "error": f"no active reminder #{rid}"}
    involved = source.author.id in (reminder.creator_id, reminder.target_user_id)
    if not involved and reminder.target_user_id != 0 and not is_admin:
        return {
            "ok": False,
            "error": "permission denied: only the creator, the target, or a server "
            "admin can cancel someone else's reminder",
        }
    await reminder_manager.mark_inactive(int(rid))
    unschedule_reminder(int(rid))
    return {"ok": True, "cancelled_reminder_id": int(rid)}


async def _do_create_reminder(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
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
        if notify:
            await source.channel.send(
                f"Sorry {source.author.mention}, I couldn't parse that time. "
                "Can you be more specific? (e.g. 'tomorrow at 9 am')"
            )
        return {"ok": False, "error": f"could not parse time {raw_dt!r}; ask for an explicit time"}

    if trigger_time <= now_utc:
        log.warning("reminder_trigger_in_past", trigger=str(trigger_time), raw=raw_dt)
        if notify:
            await source.channel.send(
                f"Sorry {source.author.mention}, I couldn't calculate that time correctly. "
                "Try again with an explicit time, e.g. 'remind me at 9:00 pm'."
            )
        return {"ok": False, "error": f"computed trigger {trigger_time} is in the past; ask for an explicit future time"}

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
        guild_id=guild_id,
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
    return {
        "ok": True,
        "reminder_id": reminder.id,
        "fires_at_utc": trigger_time.isoformat(),
        "recurring": is_recurring,
    }


async def _do_create_chore(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    cron = action.get("cron")
    if not cron:
        return {"ok": False, "error": "missing cron schedule"}
    assigned_id = await _resolve_user_id(
        action.get("assigned_to", ""), source.guild, fallback_id=None
    )
    name = action.get("name", "Unnamed chore")
    chore_id = await chore_repo.create(
        channel_id=source.channel.id,
        name=name,
        description=action.get("description", ""),
        assigned_user_id=assigned_id,
        cron_expression=cron,
        guild_id=guild_id,
    )
    return {"ok": True, "chore_id": chore_id, "name": name}


async def _do_update_profile(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    target_id = await _resolve_user_id(
        action.get("target_user", ""), source.guild, source.author.id
    )
    updates = action.get("updates") or {}
    if not updates or not target_id:
        return {"ok": False, "error": "missing updates or unknown target user"}
    if target_id != source.author.id and not is_admin:
        return {
            "ok": False,
            "error": "permission denied: only a server admin can edit another member's profile",
        }
    if not await member_repo.merge_profile(guild_id, target_id, updates):
        return {"ok": False, "error": "target user is not a registered household member"}
    return {"ok": True, "updated_keys": list(updates)}


async def _do_complete_chore(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    name = action.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "missing chore name"}
    updated = await chore_repo.complete_by_name(
        name, source.author.id if source else None, guild_id
    )
    if not updated:
        return {"ok": False, "error": f"no active chore named '{name}' — check list_chores for the exact name"}
    return {"ok": True, "completed": name}


async def _do_create_calendar_event(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    if not settings.household_calendar_id or not settings.google_service_account_json:
        if notify:
            await source.channel.send(
                "Google Calendar isn't configured yet. "
                "Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `HOUSEHOLD_CALENDAR_ID` in `.env`."
            )
        return {"ok": False, "error": "Google Calendar is not configured"}

    raw_start = action.get("start_datetime") or ""
    raw_end = action.get("end_datetime")
    start = _parse_dt(raw_start)
    end = _parse_dt(raw_end) if raw_end else None

    if not start:
        if notify:
            await source.channel.send(
                f"Sorry {source.author.mention}, I couldn't parse the event start time."
            )
        return {"ok": False, "error": f"could not parse start time {raw_start!r}"}

    # Resolve attendee @usernames → google_email from their profiles
    attendee_emails: list[str] = []
    for mention in action.get("attendees") or []:
        username = mention.lstrip("@").lower()
        profile = await member_repo.find_profile_by_name(guild_id, username)
        email = (profile or {}).get("google_email")
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
        if notify:
            await source.channel.send(
                "Hmm, I couldn't create the calendar event. "
                "Check that the service account has edit access to the household calendar."
            )
        return {"ok": False, "error": "calendar API call failed"}
    return {"ok": True, "title": action.get("title"), "invited": attendee_emails}


async def _do_update_calendar_event(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    if not settings.household_calendar_id or not settings.google_service_account_json:
        if notify:
            await source.channel.send(
                "Google Calendar isn't configured yet. "
                "Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `HOUSEHOLD_CALENDAR_ID` in `.env`."
            )
        return {"ok": False, "error": "Google Calendar is not configured"}

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
        if notify:
            await source.channel.send(
                f"Hmm, I couldn't find \"{title}\" on the calendar"
                + (" around that time" if start else "")
                + " to update it. Check the event name or time?"
            )
        return {"ok": False, "error": f"no calendar event matching '{title}' found to update"}
    return {"ok": True, "title": title}


async def _do_delete_calendar_event(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    if not settings.household_calendar_id or not settings.google_service_account_json:
        if notify:
            await source.channel.send(
                "Google Calendar isn't configured yet. "
                "Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `HOUSEHOLD_CALENDAR_ID` in `.env`."
            )
        return {"ok": False, "error": "Google Calendar is not configured"}

    title = action.get("title", "").strip()
    raw_start = action.get("start_datetime")
    start = _parse_dt(raw_start) if raw_start else None

    ok = await google_calendar.delete_event(title=title, start=start)
    if not ok:
        if notify:
            await source.channel.send(
                f"Hmm, I couldn't find \"{title}\" on the calendar"
                + (" around that time" if start else "")
                + ". Double-check the event name or time?"
            )
        return {"ok": False, "error": f"no calendar event matching '{title}' found to delete"}
    return {"ok": True, "deleted": title}


async def _do_speak_in_voice(
    action: dict, source: discord.Message, notify: bool = True,
    guild_id: int = 0, is_admin: bool = False,
) -> dict:
    text = action.get("message", "").strip()
    if not text:
        return {"ok": False, "error": "missing message"}

    target_id = await _resolve_user_id(
        action.get("target_user", ""), source.guild, source.author.id
    )
    ok = await speak_in_channel(bot, source.guild, text, target_id)
    if not ok:
        return {"ok": False, "error": "could not join a voice channel to speak"}
    return {"ok": True}


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
    rows = await chore_repo.list_active(interaction.channel_id)
    if not rows:
        await interaction.response.send_message("No chores scheduled.", ephemeral=True)
        return
    lines = []
    for r in rows:
        assignee = f" — <@{r['assigned_user_id']}>" if r["assigned_user_id"] else ""
        done = f" — last done {r['last_completed']:%Y-%m-%d}" if r["last_completed"] else ""
        lines.append(f"- **{r['name']}** [{r['cron_expression']}]{assignee}{done}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="chore", description="Add a recurring household chore")
@app_commands.describe(description="Chore and schedule, e.g. 'vacuum living room every Saturday at 11am'")
async def cmd_create_chore(interaction: discord.Interaction, description: str):
    await interaction.response.defer()

    messages = [{"role": "user", "content": f"Add a chore: {description}"}]
    complexity = _analyzer.analyze(description)
    model = router.select_model(complexity)

    guild_id = interaction.guild_id or 0
    try:
        if settings.action_protocol == "tools":
            ctx = ToolContext(
                channel_id=interaction.channel_id,
                author_id=interaction.user.id,
                author_name=interaction.user.display_name,
                guild=interaction.guild,
                guild_id=guild_id,
                is_admin=_member_is_admin(interaction.guild, interaction.user.id),
                source_message=_InteractionSource(interaction),
            )
            reply_text, actions = await gemini_client.generate_with_tools(messages, model, ctx)
            # The executor already created the chore; just relay the outcome.
            if any(a.get("type") == "create_chore" for a in actions):
                await interaction.followup.send(reply_text or "Added!")
            else:
                await interaction.followup.send(
                    reply_text or "Couldn't parse a schedule from that. Try: 'vacuum living room every Saturday at 11am'",
                    ephemeral=True,
                )
            return
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
    await chore_repo.create(
        channel_id=interaction.channel_id,
        name=chore_action.get("name", "Unnamed chore"),
        description=chore_action.get("description", ""),
        assigned_user_id=assigned_id,
        cron_expression=chore_action.get("cron"),
        guild_id=guild_id,
    )
    await interaction.followup.send(reply_text or f"Added: {chore_action.get('name')}")


@bot.tree.command(name="todo", description="Add a to-do task for one or more members")
@app_commands.describe(
    title="What needs to be done",
    assigned_to="Who's responsible — name(s) separated by commas, or leave blank for unassigned",
)
async def cmd_create_todo(interaction: discord.Interaction, title: str, assigned_to: str = ""):
    user_ids = await _resolve_user_ids(assigned_to, interaction.guild)
    await todo_repo.create(
        interaction.channel_id, title.strip(), user_ids, guild_id=interaction.guild_id or 0
    )
    mentions = " ".join(f"<@{uid}>" for uid in user_ids) if user_ids else ""
    suffix = f" — {mentions}" if mentions else ""
    await interaction.response.send_message(f"Added to-do: **{title}**{suffix}")


@bot.tree.command(name="summary", description="Show all to-dos and recurring chores")
async def cmd_summary(interaction: discord.Interaction):
    todos = await todo_repo.list_active(interaction.channel_id)
    chores = await chore_repo.list_active(interaction.channel_id)

    if not todos and not chores:
        await interaction.response.send_message("Nothing on the list yet!", ephemeral=True)
        return

    lines: list[str] = []

    if todos:
        lines.append("📋 **To-Do**")
        for t in todos:
            uids = t["assigned_user_ids"] or []
            mentions = " ".join(f"<@{uid}>" for uid in uids) if uids else "*(unassigned)*"
            lines.append(f"- {t['title']} — {mentions}")

    if chores:
        if lines:
            lines.append("")
        lines.append("🔄 **Recurring Chores**")
        for r in chores:
            assignee = f"<@{r['assigned_user_id']}>" if r["assigned_user_id"] else "*(unassigned)*"
            done = f" — last done {r['last_completed']:%Y-%m-%d}" if r["last_completed"] else ""
            lines.append(f"- **{r['name']}** [{r['cron_expression']}] — {assignee}{done}")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="remove_chore", description="Remove a recurring chore")
@app_commands.describe(name="Name of the chore to remove (partial name is fine)")
async def cmd_remove_chore(interaction: discord.Interaction, name: str):
    if not _member_is_admin(interaction.guild, interaction.user.id):
        await interaction.response.send_message(
            "Removing chores needs the **Manage Server** permission — ask an admin.",
            ephemeral=True,
        )
        return
    candidates = await chore_repo.find_active(interaction.channel_id)

    match = _fuzzy_match(name, candidates)
    if not match:
        options = ", ".join(f"**{cname}**" for _, cname in candidates) or "none"
        await interaction.response.send_message(
            f"Couldn't find a chore matching **{name}**. Active chores: {options}",
            ephemeral=True,
        )
        return

    rid, rname = match
    await chore_repo.deactivate(rid)
    await interaction.response.send_message(f"Removed chore: **{rname}**")


@bot.tree.command(name="remove_todo", description="Remove a to-do task")
@app_commands.describe(title="Title of the to-do to remove (partial name is fine)")
async def cmd_remove_todo(interaction: discord.Interaction, title: str):
    candidates = await todo_repo.find_active(interaction.channel_id)

    match = _fuzzy_match(title, candidates)
    if not match:
        options = ", ".join(f"**{ctitle}**" for _, ctitle in candidates) or "none"
        await interaction.response.send_message(
            f"Couldn't find a to-do matching **{title}**. Active to-dos: {options}",
            ephemeral=True,
        )
        return

    rid, rtitle = match
    await todo_repo.deactivate(rid)
    await interaction.response.send_message(f"Removed to-do: **{rtitle}**")


@bot.tree.command(name="register", description="Register yourself as a household member")
async def cmd_register(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message(
            "Run this inside your household server so I know which household to add you to.",
            ephemeral=True,
        )
        return
    await _upsert_member(interaction.user, interaction.guild_id)
    await user_settings_repo.set_home_guild(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(
        f"Welcome, {interaction.user.display_name}! You're now registered as a household member.",
        ephemeral=True,
    )


@bot.tree.command(name="set_home", description="Make this server your home household (used for DMs)")
async def cmd_set_home(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message(
            "Run this inside the server you want as your home household.", ephemeral=True
        )
        return
    await _upsert_member(interaction.user, interaction.guild_id)
    await user_settings_repo.set_home_guild(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(
        f"Done — **{interaction.guild.name}** is now your home household. "
        "DMs to me will use this household's data.",
        ephemeral=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

class _InteractionSource:
    """Adapts a discord.Interaction to the (channel/author/guild) surface the
    action handlers expect from a discord.Message."""

    def __init__(self, interaction: discord.Interaction):
        self.channel = interaction.channel
        self.author = interaction.user
        self.guild = interaction.guild


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


async def _all_member_mentions(guild_id: int) -> str:
    parts = []
    for uid in await member_repo.active_ids(guild_id):
        user = bot.get_user(uid)
        parts.append(user.mention if user else f"<@{uid}>")
    return " ".join(parts) if parts else "@here"


def _member_is_admin(guild: Optional[discord.Guild], user_id: int) -> bool:
    """Admin = Discord Manage Server or Administrator permission in the guild.

    Derived live from Discord — no stored role column to drift."""
    if guild is None:
        return False
    member = guild.get_member(user_id)
    if member is None:
        return False
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator


async def _resolve_home_guild(user: discord.User) -> Optional[int]:
    """Which household does a DM belong to? Explicit /set_home wins; a user
    sharing exactly one guild with the bot is auto-adopted into it."""
    stored = await user_settings_repo.get_home_guild(user.id)
    if stored:
        return stored
    mutual = [g for g in bot.guilds if g.get_member(user.id)]
    if len(mutual) == 1:
        await user_settings_repo.set_home_guild(user.id, mutual[0].id)
        return mutual[0].id
    return None


def _parse_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return reminder_manager.parse_datetime(raw)


async def _upsert_member(user: Union[discord.Member, discord.User], guild_id: int):
    await member_repo.upsert(guild_id, user.id, user.name, getattr(user, "display_name", user.name))


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
        # Group reminder — ping the household this channel belongs to
        mention = await _all_member_mentions(guild.id if guild else 0)
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


# ── Tool registration (native function calling) ──────────────────────────────
# Write executors wrap the action handlers above with notify=False: in tool
# mode the model narrates outcomes itself from the returned result dicts.
# Registered at import time — the same injection direction as init_scheduler,
# so core/ never imports bot/.

_ACTION_HANDLERS.update(
    {
        "create_reminder": _do_create_reminder,
        "create_chore": _do_create_chore,
        "complete_chore": _do_complete_chore,
        "cancel_reminder": _do_cancel_reminder,
        "create_calendar_event": _do_create_calendar_event,
        "update_calendar_event": _do_update_calendar_event,
        "delete_calendar_event": _do_delete_calendar_event,
        "speak_in_voice": _do_speak_in_voice,
        "update_profile": _do_update_profile,
    }
)


def _register_tools():
    read_tools.register(registry)

    def _wrap(handler):
        async def executor(args: dict, ctx: ToolContext) -> dict:
            return await handler(
                args,
                ctx.source_message,
                notify=False,
                guild_id=ctx.guild_id,
                is_admin=ctx.is_admin,
            )

        return executor

    for name, handler in _ACTION_HANDLERS.items():
        registry.register(DECLARATIONS[name], _wrap(handler))


_register_tools()


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
