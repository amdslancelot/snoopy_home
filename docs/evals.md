# Eval harness

Snoopy Home's LLM behavior is evaluated by a hybrid harness: **deterministic
checks** (the CI gate) plus an **LLM-as-judge** quality score (report-only).
The harness drives the *real* pipeline — `ComplexityAnalyzer` → `LLMRouter` →
`GeminiClient.generate` with the production system prompt and server-side
cache — and captures the structured actions without executing them, so it
needs no Discord connection and no database.

```
evals/
  dataset/golden.jsonl    55 golden cases
  runner.py               python -m evals.runner
  adapters.py             raw output → canonical actions (survives protocol migrations)
  scorers/deterministic.py  the CI gate
  scorers/judge.py        Gemini judge, temperature 0, JSON schema
  report.py               markdown + JSON reports
  results/                committed baseline reports
```

## Dataset format

One JSON object per line:

```json
{"id": "rem-rec-01", "tags": ["create_reminder", "recurring"], "history": [],
 "user_message": "remind me every weekday at 7:30 am to take my vitamins",
 "expected": {
   "tier": "low", "intent": "set_reminder",
   "actions": [{"type": "create_reminder",
                "args_subset": {"recurring": true,
                                "cron": {"$cron": "30 7 * * 1-5"},
                                "message": {"$icontains": "vitamin"}}}],
   "forbid_actions": [],
   "reply_rubric": "Confirms a recurring weekday 7:30 am reminder..."}}
```

`args_subset` values use tolerant matchers because LLM output is legitimately
nondeterministic in *form* (exact ISO strings for "in 10 minutes", cron
spacing, casing): `$exists`, `$null`, `$icontains`, `$future`,
`$relative_minutes`+`$tolerance_minutes`, `$cron`. Plain values compare
case-insensitively. See `evals/scorers/deterministic.py`.

Coverage: all 9 action types, read queries, chit-chat/advice (no-action
discipline), relay-vs-remind disambiguation, group reminders, voice, relative
times, recurring crons, ambiguous-time guardrail.

## The three layers

| Layer | What it checks | Cost | Gate? |
|---|---|---|---|
| Router eval (`tests/unit/test_router_eval.py`) | analyzer tier + intent match the values pinned in the dataset | zero (pure rules) | yes — runs in the normal pytest CI job |
| Deterministic eval (`evals/runner.py`) | emitted action types + args, forbidden actions, tier | ~55 Gemini calls | yes — pass rate ≥ 90% (threshold, not exact match, to absorb model nondeterminism) |
| Judge (`--judge`) | reply quality against each case's rubric (1–5, temperature 0, forced JSON schema) | ~55 extra flash calls | **never** — judges drift; scores land in the report only |

## CI wiring

- **Every push/PR (free):** router eval + scorer unit tests inside the
  existing `pytest` gate.
- **`.github/workflows/eval.yml`:** nightly (with judge), manual
  `workflow_dispatch`, and on PRs carrying the `eval` label. Needs the
  `GEMINI_API_KEY` repo secret. Uploads `report.json` + `report.md` as an
  artifact; exits non-zero below the 90% threshold.

Run locally:

```bash
python -m evals.runner                        # gate only
python -m evals.runner --judge                # + quality scores
python -m evals.runner --filter create_chore --limit 5 --no-cache
```

## Baseline — legacy `<action>` protocol (2026-07-18)

Full report: `evals/results/baseline-legacy.md`.

- **Deterministic: 54/55 (98.2%)** · Judge mean **4.65/5** (55 judged)
- The one deterministic failure is the harness earning its keep:
  `query-gen-02` ("when is the next dentist appointment?") made the model
  **hallucinate a `get_calendar_events` action that does not exist in the
  protocol** — the protocol is write-only, so the model invented a read. This
  is exactly the gap the function-calling migration's read tools close; the
  case stays in the dataset as a regression marker.
- Low judge scores caught real quality defects invisible to structural
  checks: an invented "9 AM" in a schedule confirmation (`chore-add-03`), a
  "fair" rotation that was 3-vs-4 days (`advice-03`).

This baseline is the bar the Phase-4 function-calling migration must meet or
beat before the legacy protocol is deleted.

## Tools protocol — post-migration (2026-07-18)

Full report: `evals/results/tools-protocol.md`. Deterministic 92.7–98.2%
across runs (committed judged run: **53/55, 96.4%**, judge **4.53/5**) —
within run-to-run variance of the legacy baseline, with read-grounding the
legacy protocol could not do at all. The migration iterations the harness
drove (relay regression, omitted `target_user`, claim-without-call) are
documented in `docs/function-calling.md`; read-tool expectations are scored
via each case's `expect_reads` (only for data genuinely absent from the
system-prompt household context — reminders and calendar), and incidental
profile-learning is absorbed by `optional_actions`.

## Re-pinning router expectations

`tier`/`intent` in the dataset pin the *current* analyzer behavior
(regression detection, not aspiration). After a deliberate router change:

```bash
python - <<'EOF'
import json, pathlib
from core.message_parser import ComplexityAnalyzer
p = pathlib.Path("evals/dataset/golden.jsonl"); a = ComplexityAnalyzer(); out = []
for line in p.read_text().splitlines():
    c = json.loads(line); r = a.analyze(c["user_message"])
    c["expected"]["tier"] = r.tier.value; c["expected"]["intent"] = r.detected_intent
    out.append(json.dumps(c, ensure_ascii=False))
p.write_text("\n".join(out) + "\n")
EOF
```

…and review the diff — every changed pin is a behavior change you are
choosing to accept.

## Known limitations

- **One action per reply** is a rule of the legacy protocol, so compound
  requests ("remind me at 6 AND add a chore") aren't in the dataset yet; the
  function-calling tool loop lifts this, and cases get added then.
- Judge scores are directional, not gates — a 4.6 → 4.2 drop is a signal to
  read the rationales, not a build failure.
- Time-of-day-dependent cases (e.g. "at 3 am today" being past) are excluded
  by design; the runner would flake depending on when CI runs.
