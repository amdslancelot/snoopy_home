"""
Deterministic scorer: compares captured pipeline output against a golden
case's `expected` block. No LLM involved — this is the CI gate.

Checks (each independent; a case passes when all requested checks pass):
  tier            router tier matches expected.tier
  intent          analyzer intent matches expected.intent
  actions         the multiset of emitted action types equals the expected
                  set, and each expected action's args_subset matches
  forbid_actions  none of the listed types were emitted

args_subset values are matched with tolerant normalizers, because LLM output
is legitimately nondeterministic in form (ISO datetime strings for relative
times, cron spacing, name casing):

  plain value                  case-insensitive equality for strings,
                               == for everything else
  {"$exists": true}            value present and non-null
  {"$null": true}              value absent, null, or false-y
  {"$icontains": "bins"}       case-insensitive substring
  {"$future": true}            parses as ISO datetime later than run start
  {"$relative_minutes": 10,
   "$tolerance_minutes": 3}    parses as ISO datetime within now+10min ±3min
  {"$cron": "0 11 * * 6"}      cron equality after whitespace normalization
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class CaseScore:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def normalize_cron(expr) -> str:
    return " ".join(str(expr).split())


def match_value(spec, actual, now: datetime) -> tuple[bool, str]:
    """Return (ok, detail). `spec` is a plain value or a matcher dict."""
    if isinstance(spec, dict):
        if spec.get("$exists"):
            ok = actual is not None and actual != ""
            return ok, "" if ok else "expected a non-null value, got %r" % (actual,)
        if spec.get("$null"):
            ok = not actual
            return ok, "" if ok else "expected null/absent, got %r" % (actual,)
        if "$icontains" in spec:
            ok = isinstance(actual, str) and spec["$icontains"].lower() in actual.lower()
            return ok, "" if ok else "%r does not contain %r" % (actual, spec["$icontains"])
        if spec.get("$future"):
            dt = _parse_iso(actual)
            ok = dt is not None and dt > now - timedelta(minutes=2)
            return ok, "" if ok else "%r is not a future ISO datetime" % (actual,)
        if "$relative_minutes" in spec:
            dt = _parse_iso(actual)
            if dt is None:
                return False, "%r is not an ISO datetime" % (actual,)
            target = now + timedelta(minutes=float(spec["$relative_minutes"]))
            tol = timedelta(minutes=float(spec.get("$tolerance_minutes", 2)))
            ok = abs(dt - target) <= tol
            return ok, "" if ok else "%r not within %s of %s" % (actual, tol, target.isoformat())
        if "$cron" in spec:
            ok = normalize_cron(actual) == normalize_cron(spec["$cron"])
            return ok, "" if ok else "cron %r != %r" % (actual, spec["$cron"])
        return False, "unknown matcher %r" % (spec,)

    if isinstance(spec, str) and isinstance(actual, str):
        ok = spec.lower() == actual.lower()
        return ok, "" if ok else "%r != %r" % (actual, spec)

    ok = spec == actual
    return ok, "" if ok else "%r != %r" % (actual, spec)


def _match_action(expected: dict, actual: dict, now: datetime) -> list[str]:
    problems = []
    for key, spec in (expected.get("args_subset") or {}).items():
        ok, detail = match_value(spec, actual.get(key), now)
        if not ok:
            problems.append(f"arg '{key}': {detail}")
    return problems


def score_case(case: dict, tier: str, intent: str, actions: list[dict], now: datetime) -> CaseScore:
    expected = case.get("expected", {})
    failures: list[str] = []

    if "tier" in expected and tier != expected["tier"]:
        failures.append(f"tier: got {tier!r}, expected {expected['tier']!r}")
    if "intent" in expected and intent != expected["intent"]:
        failures.append(f"intent: got {intent!r}, expected {expected['intent']!r}")

    expected_actions = expected.get("actions", [])
    actual_types = sorted(a.get("type", "") for a in actions)
    expected_types = sorted(a["type"] for a in expected_actions)

    if expected_types != actual_types:
        failures.append(f"action types: got {actual_types}, expected {expected_types}")
    else:
        remaining = list(actions)
        for exp in expected_actions:
            candidate = next((a for a in remaining if a.get("type") == exp["type"]), None)
            remaining.remove(candidate)
            problems = _match_action(exp, candidate, now)
            if problems:
                failures.append(f"{exp['type']}: " + "; ".join(problems))

    for forbidden in expected.get("forbid_actions", []):
        if any(a.get("type") == forbidden for a in actions):
            failures.append(f"forbidden action emitted: {forbidden}")

    return CaseScore(case_id=case["id"], passed=not failures, failures=failures)
