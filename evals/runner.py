"""
Eval runner: drives the REAL pipeline (ComplexityAnalyzer → LLMRouter →
GeminiClient.generate) for every golden case and scores the captured output.

Actions are captured, never executed — the runner imports nothing from bot/
and touches no database, so it is storage-agnostic by construction.

Usage:
  python -m evals.runner                       # deterministic only
  python -m evals.runner --judge               # + LLM-as-judge (report-only)
  python -m evals.runner --filter create_chore # only cases with this tag
  python -m evals.runner --limit 5 --no-cache
  python -m evals.runner --output evals/results/run1   # writes .json + .md

Exit code 0 when the deterministic pass rate ≥ --threshold (default 0.9).
Requires a real GEMINI_API_KEY.
"""

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime

from core.gemini_client import gemini_client
from core.llm_router import router
from core.message_parser import ComplexityAnalyzer
from evals.adapters import canonicalize_actions
from evals.report import render_markdown, to_json
from evals.scorers.deterministic import score_case
from evals.scorers.judge import Judge

DEFAULT_DATASET = pathlib.Path(__file__).parent / "dataset" / "golden.jsonl"

_analyzer = ComplexityAnalyzer()


def load_dataset(path: pathlib.Path) -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


async def run_case(case: dict, sem: asyncio.Semaphore, use_cache: bool, judge: Judge | None) -> dict:
    complexity = _analyzer.analyze(case["user_message"])
    model = router.select_model(complexity)
    messages = list(case.get("history", [])) + [{"role": "user", "content": case["user_message"]}]

    reply, actions, error = "", [], None
    async with sem:
        for attempt in (1, 2):
            try:
                reply, raw_actions = await gemini_client.generate(messages, model, use_cache=use_cache)
                actions = canonicalize_actions(raw_actions)
                error = None
                break
            except Exception as exc:  # transient 429/5xx: one retry with backoff
                error = str(exc)
                if attempt == 1:
                    await asyncio.sleep(15)

    now = datetime.utcnow()
    if error:
        score = None
        result = {
            "case_id": case["id"],
            "tags": case.get("tags", []),
            "user_message": case["user_message"],
            "passed": False,
            "failures": [f"pipeline error: {error}"],
        }
    else:
        score = score_case(case, complexity.tier.value, complexity.detected_intent, actions, now)
        result = {
            "case_id": case["id"],
            "tags": case.get("tags", []),
            "user_message": case["user_message"],
            "model": model,
            "tier": complexity.tier.value,
            "intent": complexity.detected_intent,
            "actions": actions,
            "reply": reply,
            "passed": score.passed,
            "failures": score.failures,
        }

    if judge is not None and not error:
        rubric = case.get("expected", {}).get("reply_rubric")
        if rubric:
            try:
                verdict = await judge.score(case["user_message"], reply, rubric)
                result["judge_score"] = verdict.get("score")
                result["judge_rationale"] = verdict.get("rationale")
            except Exception as exc:
                result["judge_score"] = None
                result["judge_rationale"] = f"judge error: {exc}"

    status = "PASS" if result["passed"] else "FAIL"
    print(f"  [{status}] {case['id']}", flush=True)
    return result


async def amain(args) -> int:
    dataset_path = pathlib.Path(args.dataset)
    cases = load_dataset(dataset_path)
    if args.filter:
        cases = [c for c in cases if args.filter in c.get("tags", [])]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    judge = Judge() if args.judge else None
    sem = asyncio.Semaphore(args.concurrency)
    print(f"Running {len(cases)} case(s) — judge={'on' if judge else 'off'} cache={not args.no_cache}")

    results = await asyncio.gather(
        *(run_case(c, sem, use_cache=not args.no_cache, judge=judge) for c in cases)
    )
    results = list(results)

    passed = sum(1 for r in results if r["passed"])
    rate = passed / len(results)
    meta = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "dataset": str(dataset_path),
        "cases": len(results),
        "passed": passed,
        "pass_rate": round(rate, 4),
        "threshold": args.threshold,
        "judge": bool(judge),
    }

    md = render_markdown(results, meta)
    print("\n" + md)

    if args.output:
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(to_json(results, meta))
        out.with_suffix(".md").write_text(md)
        print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.md')}")

    return 0 if rate >= args.threshold else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default=str(DEFAULT_DATASET))
    p.add_argument("--filter", help="only run cases carrying this tag")
    p.add_argument("--limit", type=int)
    p.add_argument("--judge", action="store_true", help="also run the LLM judge (report-only)")
    p.add_argument("--no-cache", action="store_true", help="bypass the server-side prompt cache")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("--output", help="path prefix for the .json/.md report files")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
