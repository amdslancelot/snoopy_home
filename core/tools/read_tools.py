"""
Read-tool executors: ground the model's answers in current state.

These need no Discord objects — only the repositories and the calendar
client — so they register themselves here; bot/events.py registers the
write tools (which do need Discord context).
"""

from core.tools.declarations import DECLARATIONS
from core.tools.registry import ToolContext, ToolRegistry
from integrations.google_calendar import google_calendar
from storage.repositories import chore_repo, member_repo, reminder_repo, todo_repo


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


async def _list_reminders(args: dict, ctx: ToolContext) -> dict:
    items = await reminder_repo.list_active(ctx.channel_id)
    return {
        "reminders": [
            {
                "id": r.id,
                "message": r.message,
                "recurring": r.is_recurring,
                "cron": r.cron_expression,
                "fires_at_utc": _iso(r.trigger_time) if not r.is_recurring else None,
                "voice": r.voice,
                "target_user_id": r.target_user_id,
            }
            for r in items
        ]
    }


async def _list_chores(args: dict, ctx: ToolContext) -> dict:
    rows = await chore_repo.list_active(ctx.channel_id)
    return {
        "chores": [
            {
                "name": r["name"],
                "description": r["description"],
                "cron": r["cron_expression"],
                "assigned_user_id": r["assigned_user_id"],
                "last_completed_utc": _iso(r["last_completed"]),
            }
            for r in rows
        ]
    }


async def _list_todos(args: dict, ctx: ToolContext) -> dict:
    rows = await todo_repo.list_active(ctx.channel_id)
    return {
        "todos": [
            {"title": r["title"], "assigned_user_ids": r["assigned_user_ids"]}
            for r in rows
        ]
    }


async def _get_member_profile(args: dict, ctx: ToolContext) -> dict:
    name = (args.get("name") or "").strip()
    profile = await member_repo.find_profile_by_name(ctx.guild_id, name)
    if profile is None:
        return {"ok": False, "error": f"no household member matching '{name}'"}
    return {"name": name, "profile": profile}


async def _list_calendar_events(args: dict, ctx: ToolContext) -> dict:
    days = int(args.get("days_ahead") or 7)
    events = await google_calendar.list_events(days_ahead=days)
    if events is None:
        return {"ok": False, "error": "Google Calendar is not configured"}
    return {"events": events}


async def _chore_stats(args: dict, ctx: ToolContext) -> dict:
    days = int(args.get("days") or 7)
    return {
        "days": days,
        "completions_by_member": await chore_repo.stats(ctx.guild_id, days),
    }


def register(registry: ToolRegistry) -> None:
    for name, executor in [
        ("list_reminders", _list_reminders),
        ("list_chores", _list_chores),
        ("list_todos", _list_todos),
        ("get_member_profile", _get_member_profile),
        ("list_calendar_events", _list_calendar_events),
        ("chore_stats", _chore_stats),
    ]:
        registry.register(DECLARATIONS[name], executor)
