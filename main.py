import asyncio

from config import settings
from core.observability import configure_logging, get_logger
from storage.database import db_ping, init_db


async def main():
    configure_logging()
    log = get_logger("main")

    await init_db()

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


if __name__ == "__main__":
    asyncio.run(main())
