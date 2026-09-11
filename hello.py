import asyncio

from aiogram import Bot, Dispatcher

from config import (
    TOKEN,
    BOOT_MESSAGE_IDS,
)
from database import database
from handlers import router


async def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set"
        )

    database().close()

    bot = Bot(
        token=TOKEN,
    )

    for user_id in BOOT_MESSAGE_IDS:
        try:
            await bot.send_message(
                user_id,
                "اشتغل البوت مرتلخ مولاي\nمواح",
            )
        except Exception:
            pass

    dispatcher = Dispatcher()

    dispatcher.include_router(
        router
    )

    await dispatcher.start_polling(
        bot,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )


if __name__ == "__main__":
    asyncio.run(main())