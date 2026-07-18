import asyncio

from config import settings
from core.observability import configure_logging, get_logger
from storage.migrate import run_migrations
from storage.pool import close_pool, db_ping, init_pool


async def main():
    configure_logging()
    log = get_logger("main")

    await run_migrations()
    await init_pool()

    # Import events to register all @bot.event and @bot.tree.command decorators.
    import bot.events  # noqa: F401
    from bot.client import bot
    from tasks.scheduler import is_running as scheduler_running
    from web.health import start_health_server

    runner = await start_health_server(
        bot=bot,
        db_ping=db_ping,
        scheduler_running=scheduler_running,
        port=settings.metrics_port,
    )
    log.info("health_server_started", port=settings.metrics_port)

    try:
        await bot.start(settings.discord_token)
    finally:
        await runner.cleanup()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
