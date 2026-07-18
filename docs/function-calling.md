# Native function calling

The bot's LLM pipeline runs on Gemini native function calling
(`ACTION_PROTOCOL=tools`, the default). The previous protocol — JSON inside
`<action></action>` tags, extracted by regex — remains available behind
`ACTION_PROTOCOL=legacy` for one release as a rollback switch.

## Why

The legacy protocol had two structural problems the eval harness made
measurable:

1. **Write-only.** The model could create data but never look anything up,
   so questions about current state ran ungrounded. The baseline eval caught
   it hallucinating a `get_calendar_events` action that didn't exist
   (`docs/evals.md`).
2. **Regex-and-pray.** No schema validation, malformed JSON silently
   dropped, one action per reply maximum.

## Architecture

```
core/tools/
  declarations.py   15 FunctionDeclarations (9 writes + 6 reads)
  registry.py       ToolRegistry (declaration + async executor), ToolContext
  read_tools.py     read executors (repositories + calendar — no Discord)
core/gemini_client.py
  generate_with_tools()   the loop: generate → execute calls → feed
                          function_responses back → repeat (cap 5)
bot/events.py
  _register_tools()       write executors = the existing action handlers,
                          wrapped with notify=False and registered at import
                          time (same injection direction as init_scheduler —
                          core/ never imports bot/)
```

The 6 read tools are the new capability: `list_reminders`, `list_chores`,
`list_todos`, `get_member_profile`, `list_calendar_events`, `chore_stats`
(backed by the `chore_completions` log table, migration 002 — "who did the
most chores last week?" is now answerable with data).

Write executors return result dicts (`{"ok": true, "reminder_id": 7}` /
`{"ok": false, "error": "could not parse time ..."}`) that go straight back
to the model, which narrates the outcome itself. In legacy mode the same
handlers send their own error messages (`notify=True`).

## Prompt caching with tools

Tool declarations are baked INTO the per-model server-side cache
(`CreateCachedContentConfig(tools=...)`): a request that uses
`cached_content` may not also pass `tools` or `system_instruction`, so the
cached path sends only the conversation, and the uncached fallback passes
both inline. Both paths, plus the 4096-token cache floor after deleting the
~220-line legacy protocol section, are verified against the real API by
`tests/integration/test_gemini_tools_live.py` (live marker).

The system prompt is composed from shared sections + a protocol-specific
Section 4; the tools variant is ~85% smaller because the schemas travel as
declarations, not prose.

## Loop semantics

- Cap of 5 iterations; parallel calls in one turn all execute.
- Executor exceptions become `{"ok": false, "error": ...}` function
  responses — the model apologises instead of the pipeline crashing.
- Every iteration is instrumented (latency, tokens, cost,
  `action_executions_total{action,status}`).
- Evals run the loop with `dry_run=True` contexts: calls are recorded, not
  executed; read tools return shaped empty results (a bare `{"ok": true}`
  stub measurably induced the model to hallucinate data).

## Eval results (vs the legacy baseline)

| | Legacy `<action>` | Native tools |
|---|---|---|
| Deterministic | 54/55 (98.2%) | 92.7–98.2% across runs (committed run: 53/55, 96.4%) |
| Judge mean | 4.65/5 | 4.53/5 |
| Read grounding | impossible (hallucinated a fake action) | `expect_reads` cases pass |

Findings the harness surfaced during the migration, each fixed and kept as
regression cases:

- **Relay regression**: "tell my wife X" briefly became a reminder; fixed
  with an explicit relay example in the prompt.
- **Omitted required-by-meaning args**: "remind *us*" produced a call with
  no `target_user` while the text said "everyone" — fixed by making
  `target_user` required with an `@me` sentinel.
- **Claim-without-call**: occasionally "Done!" with no tool call at all —
  mitigated with an explicit prompt rule; residual nondeterminism is why
  the CI gate is a 90% threshold, not exact match.

## Rollback

`ACTION_PROTOCOL=legacy` restores the old prompt section and regex path
end-to-end. Planned removal: one release after tools ships to prod.
