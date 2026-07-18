"""
Tool registry for Gemini native function calling.

A ToolSpec pairs a FunctionDeclaration (the schema Gemini sees) with an
async executor. Executors receive (args: dict, ctx: ToolContext) and return
a JSON-serializable dict that is fed back to the model as the function
response.

Write-tool executors live in bot/events.py and are registered at startup
(same injection pattern as tasks.scheduler.init_scheduler — core never
imports bot). Read-tool executors live in core/tools/read_tools.py.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from google.genai import types


@dataclass
class ToolContext:
    """Runtime context handed to every tool executor."""

    channel_id: int
    author_id: int
    author_name: str = ""
    guild: Any = None            # discord.Guild in the bot; None in DMs/evals
    source_message: Any = None   # discord.Message in the bot; None in evals
    dry_run: bool = False        # evals: record the call, execute nothing


@dataclass
class ToolSpec:
    declaration: types.FunctionDeclaration
    executor: Callable[[dict, ToolContext], Awaitable[dict]]


# Shaped empty results for dry-run (eval) contexts: a bare {"ok": true} stub
# invites the model to hallucinate data; an explicit empty collection makes
# it answer "nothing scheduled" honestly.
_DRY_RUN_RESULTS: dict[str, dict] = {
    "list_reminders": {"reminders": []},
    "list_chores": {"chores": []},
    "list_todos": {"todos": []},
    "get_member_profile": {"ok": False, "error": "no member by that name"},
    "list_calendar_events": {"events": []},
    "chore_stats": {"days": 7, "completions_by_member": []},
}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, declaration: types.FunctionDeclaration, executor) -> None:
        self._tools[declaration.name] = ToolSpec(declaration, executor)

    def names(self) -> list[str]:
        return list(self._tools)

    def as_tool(self) -> types.Tool:
        """All declarations bundled as the single Tool passed to Gemini."""
        return types.Tool(
            function_declarations=[spec.declaration for spec in self._tools.values()]
        )

    async def execute(self, name: str, args: dict, ctx: ToolContext) -> dict:
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        if ctx.dry_run:
            return _DRY_RUN_RESULTS.get(name, {"ok": True, "dry_run": True})
        result = await spec.executor(args, ctx)
        return result if isinstance(result, dict) else {"ok": True}


registry = ToolRegistry()
