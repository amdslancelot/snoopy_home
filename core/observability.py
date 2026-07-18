"""
Structured logging (structlog) and Prometheus metrics.

This module is the single home for observability primitives. It imports
nothing from bot/ so every layer (core, tasks, storage, integrations, web)
can use it without circular imports.

Usage:
    from core.observability import get_logger, metrics
    log = get_logger("reminder")
    log.info("reminder_created", reminder_id=7, target=user_id)
    metrics.reminders_fired_total.inc()
"""

import logging
import sys

import structlog
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram

from config import settings


def configure_logging() -> None:
    """Configure stdlib logging (discord.py, apscheduler) and structlog (our logs).

    LOG_FORMAT=json emits one JSON object per line (production);
    anything else uses the human-friendly console renderer (development).
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(levelname)s %(name)s: %(message)s",
    )

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
    ]
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    """Return a bound structlog logger tagged with the component name."""
    return structlog.get_logger(component=name)


class Metrics:
    """All Prometheus metrics for the bot.

    The module-level `metrics` singleton registers against the default
    registry; tests instantiate `Metrics(CollectorRegistry())` so repeated
    instantiation never collides.
    """

    def __init__(self, registry: CollectorRegistry = REGISTRY):
        self.registry = registry

        self.llm_request_duration_seconds = Histogram(
            "llm_request_duration_seconds",
            "Wall-clock latency of Gemini generate calls",
            ["model"],
            registry=registry,
            buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32),
        )
        self.llm_requests_total = Counter(
            "llm_requests_total",
            "Gemini generate calls by outcome",
            ["model", "status"],
            registry=registry,
        )
        self.llm_tokens_total = Counter(
            "llm_tokens_total",
            "Gemini token usage by kind (prompt/cached/candidates/thoughts)",
            ["model", "kind"],
            registry=registry,
        )
        self.llm_cost_usd_total = Counter(
            "llm_cost_usd_total",
            "Estimated Gemini spend in USD (from the configured price table)",
            ["model"],
            registry=registry,
        )
        self.cache_events_total = Counter(
            "gemini_cache_events_total",
            "Server-side prompt-cache lifecycle events",
            ["event"],
            registry=registry,
        )
        self.action_executions_total = Counter(
            "action_executions_total",
            "Structured actions executed by the bot",
            ["action", "status"],
            registry=registry,
        )
        self.router_tier_total = Counter(
            "router_tier_total",
            "Complexity-router tier decisions",
            ["tier"],
            registry=registry,
        )
        self.reminders_fired_total = Counter(
            "reminders_fired_total",
            "Reminders fired by the scheduler",
            registry=registry,
        )
        self.discord_events_total = Counter(
            "discord_events_total",
            "Discord gateway events handled",
            ["event"],
            registry=registry,
        )

    def record_llm_usage(self, model: str, usage) -> float:
        """Record token counts and estimated cost from response.usage_metadata.

        Returns the estimated cost in USD (0.0 when the model has no price
        entry). `prompt_token_count` includes cached tokens, so billable
        uncached input is prompt minus cached.
        """
        prompt = getattr(usage, "prompt_token_count", None) or 0
        cached = getattr(usage, "cached_content_token_count", None) or 0
        candidates = getattr(usage, "candidates_token_count", None) or 0
        thoughts = getattr(usage, "thoughts_token_count", None) or 0

        for kind, count in (
            ("prompt", prompt),
            ("cached", cached),
            ("candidates", candidates),
            ("thoughts", thoughts),
        ):
            if count:
                self.llm_tokens_total.labels(model=model, kind=kind).inc(count)

        prices = settings.model_prices.get(model)
        if not prices:
            return 0.0

        uncached_input = max(prompt - cached, 0)
        output = candidates + thoughts
        cost = (
            uncached_input * prices.get("input", 0.0)
            + cached * prices.get("cached", 0.0)
            + output * prices.get("output", 0.0)
        ) / 1_000_000
        if cost > 0:
            self.llm_cost_usd_total.labels(model=model).inc(cost)
        return cost


metrics = Metrics()
