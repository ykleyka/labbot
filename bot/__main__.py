import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import BOT_TOKEN, DB_PATH, GOOGLE_CREDENTIALS
from .db import Database
from .handlers import admin, common, fallback, member
from .middlewares import MemberMiddleware
from .queue_service import QueueService
from .scheduler import lessons_watcher
from .sheets import SheetsClient

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="queue", description="Очереди по предметам"),
    BotCommand(command="lessons", description="Ближайшие пары"),
    BotCommand(command="creategroup", description="Создать группу (для старосты)"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
    BotCommand(command="logout", description="Выйти из бота"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен.")

    db = Database(DB_PATH)
    await db.connect()
    qs = QueueService(db)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["qs"] = qs
    dp["sheets"] = SheetsClient(GOOGLE_CREDENTIALS)

    dp.message.outer_middleware(MemberMiddleware())
    dp.callback_query.outer_middleware(MemberMiddleware())
    # Порядок важен: fallback последним
    dp.include_routers(common.router, member.router, admin.router, fallback.router)

    await bot.set_my_commands(COMMANDS)
    watcher = asyncio.create_task(lessons_watcher(bot, db, qs))
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
