"""
Message parser and complexity analyzer.

ComplexityAnalyzer scores each incoming message across six independent
dimensions (0–12 total) and maps the score to a model tier:

  Dimension            Max   What it measures
  ─────────────────────────────────────────────────────────────────────
  token_estimate         2   Message length (word count as token proxy)
  reasoning_depth        3   Analytical / explanatory vocabulary
  multi_step             2   Chained operations or enumerated sub-tasks
  temporal_complexity    2   Simple point-in-time vs. complex recurrence
  context_dependency     1   Back-references that require prior history
  domain_complexity      2   Task sophistication derived from intent
  ─────────────────────────────────────────────────────────────────────
  TOTAL                 12

  Score  Tier    Model
  ─────────────────────────────────────────────
  0 – 3  LOW     gemini-2.0-flash   (fast, cheap)
  4 – 7  MEDIUM  gemini-2.5-flash   (balanced)
  8 – 12 HIGH    gemini-2.5-pro     (deep reasoning)
"""

import re
from dataclasses import dataclass
from enum import Enum


class ModelTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ComplexityResult:
    score: int
    tier: ModelTier
    dimensions: dict[str, int]
    detected_intent: str

    @property
    def summary(self) -> str:
        active = {k: v for k, v in self.dimensions.items() if v}
        breakdown = ", ".join(f"{k}={v}" for k, v in active.items())
        return f"score={self.score} tier={self.tier.value} intent={self.detected_intent} [{breakdown}]"


@dataclass
class ParsedMessage:
    raw_text: str
    clean_text: str        # bot mention stripped
    complexity: ComplexityResult
    model: str             # resolved model name


class ComplexityAnalyzer:
    # ── Dimension 2: reasoning_depth ──────────────────────────────────────
    # Analytical vocabulary signals that the LLM needs deeper reasoning.
    _REASONING_KW = frozenset({
        "analyze", "analyse", "explain", "why", "how does", "how do",
        "compare", "contrast", "evaluate", "best way", "pros and cons",
        "should i", "should we", "advise", "recommend", "reason",
        "cause", "effect", "difference between", "what if",
        "hypothetically", "trade-off", "tradeoff", "suggest",
        "strategy", "approach", "consider", "implications",
        "is it better", "which is better", "how should",
    })

    # ── Dimension 3: multi_step ────────────────────────────────────────────
    # Connectors that signal chained sub-tasks in a single message.
    _MULTISTEP_RE = re.compile(
        r"\band then\b"
        r"|\bafter (that|this|which)\b"
        r"|\bfirst\b.{0,80}\bthen\b"
        r"|\bstep[\s\-]by[\s\-]step\b"
        r"|\b[123][.)]\s"                  # numbered list items
        r"|\bfollowed by\b"
        r"|\band also\b"
        r"|\bmoreover\b"
        r"|\bfinally\b"
        r"|\bbefore (doing|that|this)\b",
        re.IGNORECASE,
    )

    # ── Dimension 4: temporal_complexity ──────────────────────────────────
    # Simple: single point-in-time (adds 0 — reminders are already low work).
    # Complex: recurrence rules, conditionals, relative offsets need more reasoning.
    _SIMPLE_TIME_RE = re.compile(
        r"\bat \d{1,2}(:\d{2})?\s*(am|pm)?\b"
        r"|\b(tomorrow|today|tonight|this (morning|afternoon|evening))\b"
        r"|\bin \d+ (minute|hour|day)s?\b"
        r"|\b(next )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        re.IGNORECASE,
    )
    _COMPLEX_TIME_RE = re.compile(
        r"\bevery (weekday|weekend|other|[a-z]+ and [a-z]+)\b"
        r"|\bfirst (monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|day) of\b"
        r"|\blast (monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|day) of\b"
        r"|\btwice (a|per) (week|month)\b"
        r"|\b(bi-?weekly|bi-?monthly|fortnightly)\b"
        r"|\bexcept (on )?(holidays?|weekends?|[a-z]+days?)\b"
        r"|\bevery \d+ (days?|weeks?|months?)\b",
        re.IGNORECASE,
    )

    # ── Dimension 5: context_dependency ───────────────────────────────────
    # Explicit back-references that are meaningless without prior turns.
    _CONTEXT_REF_RE = re.compile(
        r"\bthat (thing|task|reminder|chore|one)\b"
        r"|\bwhat (you|i|we) (said|mentioned|talked about|discussed|agreed)\b"
        r"|\bas (mentioned|discussed|agreed|planned)\b"
        r"|\bthe (same|previous|last|above)\b"
        r"|\blike before\b"
        r"|\bpreviously\b"
        r"|\bchange (that|it|this)\b"
        r"|\bcancel (that|it|this)\b",
        re.IGNORECASE,
    )

    # ── Dimension 6: domain_complexity (derived from intent) ──────────────
    # Intent patterns ordered from most-specific to most-general to avoid
    # false positives from the broad "planning" pattern.
    _INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
        ("calendar_op",     re.compile(
            r"\b(add|create|schedule|book|put|delete|remove|update|move|reschedule|rename|edit|change)\b"
            r".{0,50}"
            r"\b(event|calendar|meeting|appointment)\b"
            r"|\b(event|calendar|meeting|appointment)\b"
            r".{0,50}"
            r"\b(add|create|schedule|book|delete|remove|update|move|reschedule)\b",
            re.IGNORECASE,
        )),
        ("set_reminder",    re.compile(
            r"\b(remind|reminder|don'?t forget|remember to|alert me|notify me)\b",
            re.IGNORECASE,
        )),
        ("set_chore",       re.compile(
            r"\b(add|create|schedule|set up|new).{0,25}(chore|task|routine)\b",
            re.IGNORECASE,
        )),
        ("complete_chore",  re.compile(
            r"\b(done|finished|completed|checked off).{0,30}(chore|task)\b"
            r"|\bmark.{0,20}(done|complete|finished)\b",
            re.IGNORECASE,
        )),
        ("query_reminders", re.compile(
            r"\b(list|show|what('?s| are)).{0,20}reminder\b",
            re.IGNORECASE,
        )),
        ("query_chores",    re.compile(
            r"\b(list|show|what('?s| are)).{0,20}chore\b"
            r"|\bchores?.{0,20}(due|today|this week)\b",
            re.IGNORECASE,
        )),
        ("relay_message",   re.compile(
            r"\b(tell|ask|let|message|relay to|pass to|inform).{0,30}"
            r"(partner|spouse|wife|husband|roommate|flatmate|housemate|them)\b",
            re.IGNORECASE,
        )),
        ("planning",        re.compile(
            r"\b(plan|organis?e|coordinate|arrange|figure out|work out|decide)\b",
            re.IGNORECASE,
        )),
    ]

    _DOMAIN_SCORE: dict[str, int] = {
        "calendar_op":     2,   # API call — needs reliable action-block emission
        "set_reminder":    0,   # deterministic — LLM just extracts + confirms
        "set_chore":       1,   # slight structuring required
        "complete_chore":  0,
        "query_reminders": 0,
        "query_chores":    0,
        "relay_message":   1,   # requires rephrasing for the recipient
        "planning":        2,   # open-ended coordination
        "general":         1,
    }

    def analyze(self, text: str) -> ComplexityResult:
        words = text.split()
        text_lower = text.lower()

        # ── 1. Token estimate (0–2) ──
        wc = len(words)
        if wc < 10:
            token_score = 0
        elif wc <= 50:
            token_score = 1
        else:
            token_score = 2

        # ── 2. Reasoning depth (0–3) ──
        # Count distinct keyword matches (substring search on lowered text).
        hits = sum(1 for kw in self._REASONING_KW if kw in text_lower)
        reasoning_score = min(hits, 3)

        # ── 3. Multi-step (0–2) ──
        ms_hits = len(self._MULTISTEP_RE.findall(text))
        multistep_score = min(ms_hits, 2)

        # ── 4. Temporal complexity (0–2) ──
        # Simple time contributes 0 because reminder creation is already cheap.
        if self._COMPLEX_TIME_RE.search(text):
            temporal_score = 2
        else:
            temporal_score = 0

        # ── 5. Context dependency (0–1) ──
        # Require ≥2 back-reference signals to avoid false positives on
        # normal demonstratives ("this weekend", "that time").
        ctx_hits = len(self._CONTEXT_REF_RE.findall(text))
        context_score = min(ctx_hits // 2, 1)

        # ── 6. Domain complexity (0–2) ──
        intent = self._detect_intent(text)
        domain_score = self._DOMAIN_SCORE.get(intent, 1)

        total = (
            token_score + reasoning_score + multistep_score
            + temporal_score + context_score + domain_score
        )

        if total >= 8:
            tier = ModelTier.HIGH
        elif total >= 4:
            tier = ModelTier.MEDIUM
        else:
            tier = ModelTier.LOW

        # Calendar operations require reliable action-block emission — never use LOW tier.
        if intent == "calendar_op" and tier == ModelTier.LOW:
            tier = ModelTier.MEDIUM

        return ComplexityResult(
            score=total,
            tier=tier,
            dimensions={
                "token_estimate":      token_score,
                "reasoning_depth":     reasoning_score,
                "multi_step":          multistep_score,
                "temporal_complexity": temporal_score,
                "context_dependency":  context_score,
                "domain_complexity":   domain_score,
            },
            detected_intent=intent,
        )

    def _detect_intent(self, text: str) -> str:
        for name, pattern in self._INTENT_PATTERNS:
            if pattern.search(text):
                return name
        return "general"
