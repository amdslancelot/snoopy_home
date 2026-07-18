"""Render eval results as JSON (machine) and Markdown (humans/CI artifact)."""

import json
from collections import defaultdict
from datetime import datetime


def to_json(results: list[dict], meta: dict) -> str:
    return json.dumps({"meta": meta, "results": results}, indent=2, default=str)


def render_markdown(results: list[dict], meta: dict) -> str:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    rate = passed / total if total else 0.0

    judged = [r for r in results if r.get("judge_score") is not None]
    judge_avg = sum(r["judge_score"] for r in judged) / len(judged) if judged else None

    by_tag: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        for tag in r.get("tags", []) or ["untagged"]:
            by_tag[tag].append(r)

    lines = [
        f"# Eval report — {meta.get('timestamp', datetime.utcnow().isoformat())}",
        "",
        f"- Dataset: `{meta.get('dataset')}` ({total} cases)",
        f"- Deterministic pass rate: **{passed}/{total} ({rate:.1%})** "
        f"(threshold {meta.get('threshold'):.0%})",
    ]
    if judge_avg is not None:
        lines.append(f"- Judge mean score: **{judge_avg:.2f} / 5** ({len(judged)} judged)")
    lines += ["", "## By tag", "", "| tag | passed | total |", "|---|---|---|"]
    for tag in sorted(by_tag):
        rs = by_tag[tag]
        lines.append(f"| {tag} | {sum(1 for r in rs if r['passed'])} | {len(rs)} |")

    failures = [r for r in results if not r["passed"]]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines.append(f"### {r['case_id']}")
            lines.append(f"- message: `{r['user_message']}`")
            for f in r["failures"]:
                lines.append(f"- ✗ {f}")
            if r.get("reply"):
                lines.append(f"- reply: {r['reply'][:300]}")
            lines.append("")

    low_judge = [r for r in judged if r["judge_score"] is not None and r["judge_score"] <= 2]
    if low_judge:
        lines += ["", "## Low judge scores (≤2)", ""]
        for r in low_judge:
            lines.append(f"- **{r['case_id']}** ({r['judge_score']}/5): {r.get('judge_rationale', '')}")

    return "\n".join(lines) + "\n"
