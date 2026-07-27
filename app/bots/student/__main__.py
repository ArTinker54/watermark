"""Точка входа student-бота: ``python -m app.bots.student``."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from app.bots.middlewares import DbSessionMiddleware
from app.bots.student.handlers import build_router
from app.config import get_settings
from app.db import create_database
from app.logging_setup import setup_logging

logger = logging.getLogger(__name__)

_COMMANDS = [
    BotCommand(command="start", description="Регистрация и условия"),
    BotCommand(command="id", description="Мой номер участника"),
    BotCommand(command="offer", description="Условия доступа"),
    BotCommand(command="help", description="Справка"),
]


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    database = create_database(settings)
    await database.create_all()

    bot = Bot(
        token=settings.student_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings
    dispatcher.update.outer_middleware(DbSessionMiddleware(database.session_factory))
    dispatcher.include_router(build_router())

    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.error("Telegram отклонил STUDENT_BOT_TOKEN — проверьте .env")
            raise SystemExit(1) from None

        logger.info("student-bot запущен: @%s", me.username)
        await bot.set_my_commands(_COMMANDS)
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        # Закрывать надо и при падении на старте, иначе aiohttp ругается
        # на незакрытую сессию поверх настоящей причины ошибки.
        await bot.session.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
