"""Tests for core/observability.py (metrics + cost) and web/health.py (endpoints)."""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry

from core.observability import Metrics
from web.health import build_app


class _Usage:
    def __init__(self, prompt=0, cached=0, candidates=0, thoughts=0):
        self.prompt_token_count = prompt
        self.cached_content_token_count = cached
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


class _StubBot:
    def __init__(self, ready: bool):
        self._ready = ready

    def is_ready(self) -> bool:
        return self._ready


# ── Metrics ───────────────────────────────────────────────────────────────────

def test_record_llm_usage_counts_tokens_by_kind():
    reg = CollectorRegistry()
    m = Metrics(reg)
    m.record_llm_usage("gemini-2.5-flash", _Usage(prompt=1000, cached=400, candidates=100, thoughts=50))

    def val(kind):
        return reg.get_sample_value(
            "llm_tokens_total", {"model": "gemini-2.5-flash", "kind": kind}
        )

    assert val("prompt") == 1000
    assert val("cached") == 400
    assert val("candidates") == 100
    assert val("thoughts") == 50


def test_record_llm_usage_cost_math():
    m = Metrics(CollectorRegistry())
    cost = m.record_llm_usage(
        "gemini-2.5-flash", _Usage(prompt=1000, cached=400, candidates=100, thoughts=50)
    )
    # uncached input = 600 @ $0.30/M, cached = 400 @ $0.075/M, output = 150 @ $2.50/M
    expected = (600 * 0.30 + 400 * 0.075 + 150 * 2.50) / 1_000_000
    assert cost == pytest.approx(expected)


def test_record_llm_usage_unknown_model_costs_nothing():
    reg = CollectorRegistry()
    m = Metrics(reg)
    cost = m.record_llm_usage("some-unknown-model", _Usage(prompt=500, candidates=100))
    assert cost == 0.0
    # tokens are still recorded even without a price entry
    assert reg.get_sample_value(
        "llm_tokens_total", {"model": "some-unknown-model", "kind": "prompt"}
    ) == 500


def test_record_llm_usage_missing_fields_tolerated():
    m = Metrics(CollectorRegistry())
    cost = m.record_llm_usage("gemini-2.5-pro", object())  # no usage attrs at all
    assert cost == 0.0


def test_fresh_registries_do_not_collide():
    Metrics(CollectorRegistry())
    Metrics(CollectorRegistry())  # would raise on duplicate registration if shared


# ── Health endpoints ──────────────────────────────────────────────────────────

async def _client_for(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_health_always_ok():
    app = build_app(bot=_StubBot(False), registry=CollectorRegistry())
    client = await _client_for(app)
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"
    finally:
        await client.close()


async def test_ready_ok_when_all_checks_pass():
    async def ping_ok():
        return True

    app = build_app(
        bot=_StubBot(True),
        db_ping=ping_ok,
        scheduler_running=lambda: True,
        registry=CollectorRegistry(),
    )
    client = await _client_for(app)
    try:
        resp = await client.get("/ready")
        body = await resp.json()
        assert resp.status == 200
        assert body["checks"] == {"discord": True, "database": True, "scheduler": True}
    finally:
        await client.close()


async def test_ready_503_when_db_down():
    async def ping_fail():
        raise RuntimeError("db down")

    app = build_app(
        bot=_StubBot(True),
        db_ping=ping_fail,
        scheduler_running=lambda: True,
        registry=CollectorRegistry(),
    )
    client = await _client_for(app)
    try:
        resp = await client.get("/ready")
        body = await resp.json()
        assert resp.status == 503
        assert body["checks"]["database"] is False
    finally:
        await client.close()


async def test_metrics_endpoint_exposes_counters():
    reg = CollectorRegistry()
    m = Metrics(reg)
    m.router_tier_total.labels(tier="LOW").inc()

    app = build_app(bot=_StubBot(True), registry=reg)
    client = await _client_for(app)
    try:
        resp = await client.get("/metrics")
        text = await resp.text()
        assert resp.status == 200
        assert 'router_tier_total{tier="LOW"} 1.0' in text
    finally:
        await client.close()
