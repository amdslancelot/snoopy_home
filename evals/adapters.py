"""
Adapters that map raw pipeline output to canonical action dicts.

The golden dataset expresses expectations against *canonical* actions
(`{"type": ..., <args>}`). Today the pipeline's legacy `<action>` protocol
already emits that shape; after the function-calling migration, tool calls
are mapped here to the same shape — so the dataset survives the migration
unchanged and before/after scores are directly comparable.
"""


def canonicalize_actions(raw_actions: list) -> list[dict]:
    """Legacy protocol: action dicts pass through; junk is dropped."""
    return [a for a in raw_actions if isinstance(a, dict) and a.get("type")]


def canonicalize_tool_calls(tool_calls: list) -> list[dict]:
    """Function-calling protocol (Phase 4): (name, args) pairs → canonical dicts."""
    out = []
    for call in tool_calls:
        name = getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else None)
        args = getattr(call, "args", None) or (call.get("args") if isinstance(call, dict) else {})
        if name:
            out.append({"type": name, **dict(args or {})})
    return out
