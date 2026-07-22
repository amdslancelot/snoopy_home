# Observability

Snoopy Home ships structured logging (structlog), Prometheus metrics, and
health endpoints. Everything lives in `core/observability.py` (logging +
metric definitions) and `web/health.py` (HTTP surface); `main.py` wires them
together at startup.

## Logging

All application logs go through structlog. `LOG_FORMAT=json` (production)
emits one JSON object per line to stdout — journald/Loki-friendly;
`LOG_FORMAT=console` (default, development) renders human-readable colored
output. `LOG_LEVEL` controls both structlog and stdlib logging (discord.py,
APScheduler).

Events are named, not written as sentences — `reminder_created`,
`llm_response`, `action_failed` — with context as key-value fields, so logs
are grep-able and queryable:

```json
{"component": "gemini", "event": "llm_response", "model": "gemini-2.5-flash",
 "duration_s": 1.42, "cached": true, "cost_usd": 0.000113, "level": "info",
 "timestamp": "2026-07-18T09:30:00Z"}
```

## HTTP endpoints (port `METRICS_PORT`, default 8080)

| Path | Purpose | Semantics |
|---|---|---|
| `/health` | liveness | 200 as long as the process and event loop are alive. Kubernetes restarts the pod when this fails. |
| `/ready` | readiness | 200 only when Discord is connected, the DB answers `SELECT 1`, and APScheduler is running; 503 with a per-check JSON body otherwise. |
| `/metrics` | Prometheus | standard exposition format. |

The server runs on the bot's own event loop (aiohttp `TCPSite`) — no extra
thread, and a wedged event loop makes `/health` time out, which is exactly
the signal a liveness probe wants.

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `llm_request_duration_seconds` | model | Gemini call latency histogram |
| `llm_requests_total` | model, status | calls by outcome (success/error) |
| `llm_tokens_total` | model, kind | token usage; kind = prompt / cached / candidates / thoughts (from `usage_metadata`) |
| `llm_cost_usd_total` | model | estimated spend, computed from the `MODEL_PRICES` table in `config.py` |
| `gemini_cache_events_total` | event | prompt-cache lifecycle: created / create_failed / hit / uncached |
| `action_executions_total` | action, status | structured actions executed by the bot |
| `router_tier_total` | tier | complexity-router decisions (LOW/MEDIUM/HIGH) |
| `reminders_fired_total` | — | reminders fired by the scheduler |
| `discord_events_total` | event | gateway events handled (ready/message/member_join) |

Cost accounting note: `prompt_token_count` includes cached tokens, so the
billable uncached input is `prompt − cached`; output cost covers
`candidates + thoughts`. Prices in `config.py` are **approximate** — verify
against <https://ai.google.dev/gemini-api/docs/pricing> and override with the
`MODEL_PRICES` env var (JSON) when Google reprices.

## Local Prometheus + Grafana (optional, via podman)

```bash
# Prometheus scraping the bot on the host
podman run -d --name prom -p 9090:9090 \
  -v ./deploy/prometheus-local.yml:/etc/prometheus/prometheus.yml:Z \
  prom/prometheus

# Grafana; import deploy/grafana/snoopy-dashboard.json
podman run -d --name grafana -p 3000:3000 grafana/grafana
```

Minimal `prometheus-local.yml`:

```yaml
scrape_configs:
  - job_name: snoopy
    static_configs:
      - targets: ["host.containers.internal:8080"]
```

## Kubernetes

The k3s deployment (see `deploy/PLAN-DEPLOY-K3S.md`) points `livenessProbe` at
`/health` and `readinessProbe` at `/ready` on containerPort 8080. Scraping
can be enabled later with a `prometheus.io/scrape: "true"` pod annotation or
a ServiceMonitor once a Prometheus stack exists on the cluster; the port is
cluster-internal only and never exposed through the Ingress.

## Testing

`tests/unit/test_observability.py` covers the cost/token accounting (fresh
`CollectorRegistry` per test so registrations never collide) and drives the
three endpoints through `aiohttp.test_utils` with stubbed checks.
