"""
Household-context formatting.

Since multi-tenancy, the household block (roster + chore schedule) is NOT
part of the cached system prompt — one server-side cache per guild would
multiply the 4096-token cache floor by the number of guilds. Instead the
block rides as a small leading context message on every request
(~100-500 tokens, uncached), built fresh from the database per message.
"""

import json as _json

_TEMPLATE = """## Household Members
{members}

## Active Chore Schedule
{chores}
"""

PREAMBLE = "[HOUSEHOLD DATA — provided by the system, not a user message]\n"


def format_household(members: list[dict], chores: list[dict]) -> str:
    """Render the household block. `profile` may be a dict (JSONB) or a JSON string."""

    def _member_line(m: dict) -> str:
        profile = m.get("profile") or {}
        if isinstance(profile, str):
            profile = _json.loads(profile or "{}")
        profile_str = " | ".join(f"{k}: {v}" for k, v in profile.items() if v)
        profile_part = f"  [{profile_str}]" if profile_str else ""
        return f"  - {m['display_name']} (@{m['username']}){profile_part}"

    m_lines = "\n".join(_member_line(m) for m in members) or "  No members registered yet."

    c_lines = "\n".join(
        "  - {name}: {desc} [{cron}]{who}".format(
            name=c["name"],
            desc=c.get("description") or "no description",
            cron=c["cron_expression"],
            who=" — assigned to @" + c["assigned_username"] if c.get("assigned_username") else "",
        )
        for c in chores
    ) or "  No chores scheduled yet."

    return _TEMPLATE.format(members=m_lines, chores=c_lines)
