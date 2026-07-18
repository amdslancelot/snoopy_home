"""Tests for the tool registry and declarations."""

from core.tools.declarations import DECLARATIONS
from core.tools.registry import ToolContext, ToolRegistry

EXPECTED_TOOLS = {
    # writes (the former <action> protocol)
    "create_reminder", "create_chore", "complete_chore", "cancel_reminder",
    "create_calendar_event", "update_calendar_event", "delete_calendar_event",
    "speak_in_voice", "update_profile",
    # reads (new)
    "list_reminders", "list_chores", "list_todos",
    "get_member_profile", "list_calendar_events", "chore_stats",
}


def test_all_fifteen_declarations_present():
    assert set(DECLARATIONS) == EXPECTED_TOOLS


def test_as_tool_bundles_registered_declarations():
    reg = ToolRegistry()

    async def noop(args, ctx):
        return {}

    reg.register(DECLARATIONS["list_chores"], noop)
    reg.register(DECLARATIONS["create_reminder"], noop)
    names = [d.name for d in reg.as_tool().function_declarations]
    assert set(names) == {"list_chores", "create_reminder"}


async def test_execute_unknown_tool_returns_error_dict():
    reg = ToolRegistry()
    result = await reg.execute("nonexistent", {}, ToolContext(channel_id=0, author_id=0))
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


async def test_dry_run_never_calls_executor():
    reg = ToolRegistry()
    called = []

    async def executor(args, ctx):
        called.append(args)
        return {"ok": True}

    reg.register(DECLARATIONS["complete_chore"], executor)
    result = await reg.execute(
        "complete_chore", {"name": "x"}, ToolContext(channel_id=0, author_id=0, dry_run=True)
    )
    assert result == {"ok": True, "dry_run": True}
    assert called == []


async def test_dry_run_read_tools_return_shaped_empties():
    """Shapeless stubs made the model hallucinate data in evals; read tools
    must return explicit empty collections in dry-run."""
    reg = ToolRegistry()

    async def executor(args, ctx):
        raise AssertionError("must not run in dry-run")

    reg.register(DECLARATIONS["list_chores"], executor)
    ctx = ToolContext(channel_id=0, author_id=0, dry_run=True)
    assert await reg.execute("list_chores", {}, ctx) == {"chores": []}


async def test_none_result_normalised_to_ok():
    reg = ToolRegistry()

    async def executor(args, ctx):
        return None

    reg.register(DECLARATIONS["complete_chore"], executor)
    result = await reg.execute("complete_chore", {"name": "x"}, ToolContext(channel_id=0, author_id=0))
    assert result == {"ok": True}
