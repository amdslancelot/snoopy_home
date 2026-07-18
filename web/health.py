"""
Tiny aiohttp server exposing:

  /health   liveness  — 200 as long as the process and event loop are alive
  /ready    readiness — 200 only when Discord is connected, the DB answers,
                        and the scheduler is running; 503 otherwise
  /metrics  Prometheus exposition

The bot instance and check callables are injected by main.py, so this
module never imports bot/ (layering rule).
"""

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _handle_ready(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    db_ping = request.app["db_ping"]
    scheduler_running = request.app["scheduler_running"]

    checks = {"discord": bool(bot and bot.is_ready())}
    try:
        checks["database"] = bool(await db_ping()) if db_ping else False
    except Exception:
        checks["database"] = False
    try:
        checks["scheduler"] = bool(scheduler_running()) if scheduler_running else False
    except Exception:
        checks["scheduler"] = False

    ok = all(checks.values())
    return web.json_response(
        {"status": "ok" if ok else "degraded", "checks": checks},
        status=200 if ok else 503,
    )


async def _handle_metrics(request: web.Request) -> web.Response:
    body = generate_latest(request.app["registry"])
    return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})


def build_app(bot=None, db_ping=None, scheduler_running=None, registry=REGISTRY) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["db_ping"] = db_ping
    app["scheduler_running"] = scheduler_running
    app["registry"] = registry
    app.add_routes(
        [
            web.get("/health", _handle_health),
            web.get("/ready", _handle_ready),
            web.get("/metrics", _handle_metrics),
        ]
    )
    return app


async def start_health_server(bot, db_ping, scheduler_running, port: int) -> web.AppRunner:
    """Start the server on the running event loop. Returns the runner for cleanup."""
    app = build_app(bot=bot, db_ping=db_ping, scheduler_running=scheduler_running)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
