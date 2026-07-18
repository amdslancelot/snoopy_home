"""
Adapters that map raw pipeline output to canonical action dicts.

The golden dataset expresses expectations against *canonical* actions
(`{"type": ..., <args>}`). The legacy `<action>` protocol and the
function-calling loop both emit that shape, so the dataset survives the
protocol migration and before/after scores are directly comparable.

Read tools (new in the function-calling protocol) are grounding lookups,
not state mutations — they are split out and scored separately via each
case's optional `expect_reads` list, so a query that now correctly calls
`list_chores` doesn't fail an `actions: []` expectation written for the
write-only legacy protocol.
"""

READ_TOOLS = {
    "list_reminders",
    "list_chores",
    "list_todos",
    "get_member_profile",
    "list_calendar_events",
    "chore_stats",
}


def split_actions(raw_actions: list) -> tuple[list[dict], list[str]]:
    """Return (write_actions, read_tool_names) from raw pipeline output."""
    writes, reads = [], []
    for a in raw_actions:
        if not (isinstance(a, dict) and a.get("type")):
            continue
        if a["type"] in READ_TOOLS:
            reads.append(a["type"])
        else:
            writes.append(a)
    return writes, reads


def canonicalize_actions(raw_actions: list) -> list[dict]:
    """Write actions only (state mutations) — what `expected.actions` describes."""
    return split_actions(raw_actions)[0]


def canonicalize_tool_calls(tool_calls: list) -> list[dict]:
    """(name, args) pairs → canonical dicts (used by external tooling/tests)."""
    out = []
    for call in tool_calls:
        name = getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else None)
        args = getattr(call, "args", None) or (call.get("args") if isinstance(call, dict) else {})
        if name:
            out.append({"type": name, **dict(args or {})})
    return out
