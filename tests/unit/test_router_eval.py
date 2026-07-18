"""
Router regression eval: every golden case's pinned tier/intent must match the
live ComplexityAnalyzer. Pure rules — runs in the normal CI gate at zero API
cost. When a deliberate router change shifts expectations, re-pin with:

    python - <<'EOF'
    # (see docs/evals.md — "Re-pinning router expectations")
    EOF
"""

import json
import pathlib

import pytest

from core.message_parser import ComplexityAnalyzer

DATASET = pathlib.Path(__file__).parents[2] / "evals" / "dataset" / "golden.jsonl"

_cases = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
_analyzer = ComplexityAnalyzer()


@pytest.mark.parametrize("case", _cases, ids=[c["id"] for c in _cases])
def test_router_matches_pinned_expectations(case):
    result = _analyzer.analyze(case["user_message"])
    expected = case["expected"]
    assert result.tier.value == expected["tier"], (
        f"tier drifted: {result.tier.value} != pinned {expected['tier']}"
    )
    assert result.detected_intent == expected["intent"], (
        f"intent drifted: {result.detected_intent} != pinned {expected['intent']}"
    )


def test_dataset_ids_unique():
    ids = [c["id"] for c in _cases]
    assert len(ids) == len(set(ids))


def test_dataset_actions_well_formed():
    for case in _cases:
        for action in case["expected"].get("actions", []):
            assert "type" in action, case["id"]
