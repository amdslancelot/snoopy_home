import asyncio

from config import settings
from storage.database import init_db


async def main():
    await init_db()

    # Import events to register all @bot.event and @bot.tree.command decorators.
    import bot.events  # noqa: F401
    from bot.client import bot

    await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
