"""
FunctionDeclarations for all 15 tools — 9 writes (the former <action>
protocol) plus 6 reads (new: the legacy protocol was write-only, so the
model had no way to ground answers about current state).

Schema style: plain dicts coerced by google-genai into types.Schema.
"""

from google.genai import types


def _fd(name: str, description: str, props: dict | None = None, required: list[str] | None = None):
    params = None
    if props:
        params = {"type": "OBJECT", "properties": props}
        if required:
            params["required"] = required
    return types.FunctionDeclaration(name=name, description=description, parameters=params)


_TARGET_USER = {
    "type": "STRING",
    "description": "Household member as @username; '@everyone' for the whole household; omit for the message author.",
}
_ISO = "ISO-8601 datetime string, e.g. 2026-07-18T21:00:00"

DECLARATIONS: dict[str, types.FunctionDeclaration] = {
    d.name: d
    for d in [
        # ── Writes ────────────────────────────────────────────────────────
        _fd(
            "create_reminder",
            "Schedule a reminder. Any 'remind <who> ... at/in <time>' request is ALWAYS "
            "a timed reminder (the time expression is the trigger, never message content).",
            {
                "target_user": {
                    "type": "STRING",
                    "description": "REQUIRED. Who gets reminded: '@everyone' when the user says "
                    "us / everyone / the gang / this channel; '@me' for the message author; "
                    "otherwise the '@username' named in the request.",
                },
                "message": {"type": "STRING", "description": "What to remind them."},
                "datetime": {"type": "STRING", "description": f"One-time trigger, {_ISO}. Omit for recurring."},
                "recurring": {"type": "BOOLEAN", "description": "True for repeating reminders."},
                "cron": {"type": "STRING", "description": "5-field cron (minute hour dom month dow), only when recurring."},
                "voice": {"type": "BOOLEAN", "description": "True when the user asks to be reminded out loud / vocally."},
            },
            required=["message", "target_user"],
        ),
        _fd(
            "create_chore",
            "Add a recurring household chore to the schedule.",
            {
                "name": {"type": "STRING", "description": "Short chore name, e.g. 'Vacuum living room'."},
                "description": {"type": "STRING"},
                "cron": {"type": "STRING", "description": "5-field cron schedule."},
                "assigned_to": {"type": "STRING", "description": "@username responsible, omit if unassigned."},
            },
            required=["name", "cron"],
        ),
        _fd(
            "complete_chore",
            "Mark an existing chore as done (case-insensitive name match). The completion "
            "is logged for fairness statistics.",
            {"name": {"type": "STRING", "description": "Stored chore name."}},
            required=["name"],
        ),
        _fd(
            "cancel_reminder",
            "Cancel an active reminder by its numeric id (shown by list_reminders / /reminders).",
            {"reminder_id": {"type": "INTEGER"}},
            required=["reminder_id"],
        ),
        _fd(
            "create_calendar_event",
            "Add an event to the shared household Google Calendar. Only when the user "
            "explicitly asks for a calendar entry.",
            {
                "title": {"type": "STRING"},
                "description": {"type": "STRING"},
                "start_datetime": {"type": "STRING", "description": _ISO},
                "end_datetime": {"type": "STRING", "description": f"{_ISO}; omit for start + 1 hour."},
                "attendees": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Household @usernames to invite; their Google emails are resolved from profiles.",
                },
            },
            required=["title", "start_datetime"],
        ),
        _fd(
            "update_calendar_event",
            "Move, rename, or edit an existing calendar event (fuzzy title match).",
            {
                "title": {"type": "STRING", "description": "Current title used to find the event."},
                "start_datetime": {"type": "STRING", "description": f"Current start ({_ISO}) to narrow the search; omit if unknown."},
                "new_title": {"type": "STRING"},
                "new_start_datetime": {"type": "STRING", "description": _ISO},
                "new_end_datetime": {"type": "STRING", "description": f"{_ISO}; omit to preserve duration."},
                "new_description": {"type": "STRING"},
            },
            required=["title"],
        ),
        _fd(
            "delete_calendar_event",
            "Remove an event from the household calendar (fuzzy title match).",
            {
                "title": {"type": "STRING"},
                "start_datetime": {"type": "STRING", "description": f"{_ISO}; omit to search the next 7 days."},
            },
            required=["title"],
        ),
        _fd(
            "speak_in_voice",
            "Join the target's voice channel and speak a message NOW via TTS. For future "
            "spoken reminders use create_reminder with voice=true instead.",
            {
                "message": {"type": "STRING"},
                "target_user": _TARGET_USER,
            },
            required=["message"],
        ),
        _fd(
            "update_profile",
            "Merge personal facts a member shares into their profile (never replaces "
            "unmentioned keys). Use whenever someone mentions a personal fact.",
            {
                "target_user": _TARGET_USER,
                "updates": {
                    "type": "OBJECT",
                    "description": "Only the keys being learned right now.",
                    "properties": {
                        "age": {"type": "INTEGER"},
                        "sex": {"type": "STRING"},
                        "height": {"type": "STRING"},
                        "wake_time": {"type": "STRING"},
                        "sleep_time": {"type": "STRING"},
                        "work_hours": {"type": "STRING"},
                        "diet": {"type": "STRING"},
                        "medications": {"type": "STRING"},
                        "health_notes": {"type": "STRING"},
                        "hobbies": {"type": "STRING"},
                        "timezone": {"type": "STRING"},
                        "google_email": {"type": "STRING"},
                        "notes": {"type": "STRING", "description": "Any other fact, free-form."},
                    },
                },
            },
            required=["updates"],
        ),
        # ── Reads (new capability) ────────────────────────────────────────
        _fd(
            "list_reminders",
            "List this channel's active reminders (id, message, when). Call before "
            "answering questions about reminders or cancelling by description.",
        ),
        _fd(
            "list_chores",
            "List this channel's active chores with schedule, assignee, and last completion.",
        ),
        _fd(
            "list_todos",
            "List this channel's open to-do items and who they are assigned to.",
        ),
        _fd(
            "get_member_profile",
            "Fetch a household member's stored profile facts.",
            {"name": {"type": "STRING", "description": "Username or display name."}},
            required=["name"],
        ),
        _fd(
            "list_calendar_events",
            "List upcoming events on the household Google Calendar.",
            {"days_ahead": {"type": "INTEGER", "description": "Look-ahead window in days (default 7)."}},
        ),
        _fd(
            "chore_stats",
            "Chore completion counts per member over a recent window — use for "
            "'who did the most chores', fairness, and rotation questions.",
            {"days": {"type": "INTEGER", "description": "Window in days (default 7)."}},
        ),
    ]
}
