"""Точка входа admin-бота: ``python -m app.bots.admin``.

Процесс держит два подключения к Telegram: своим токеном он общается с автором,
а токеном student-бота раздаёт уроки ученикам. Отдельный канал связи между
ботами не нужен — база и хранилище общие.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bots.admin.handlers import build_router
from app.bots.middlewares import AdminOnlyMiddleware, DbSessionMiddleware
from app.config import get_settings
from app.db import create_database
from app.logging_setup import setup_logging
from app.services import LessonBroadcaster, Storage, TraceService
from app.watermark import WM_BIT_LENGTH, WatermarkEngine

logger = logging.getLogger(__name__)

_COMMANDS = [
    BotCommand(command="newlesson", description="Новый урок и рассылка"),
    BotCommand(command="trace", description="Найти источник утечки"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="cancel", description="Прервать сценарий"),
]


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.admin_id_set:
        logger.error("ADMIN_IDS пуст: бот запустится, но не ответит никому")

    database = create_database(settings)
    await database.create_all()

    storage = Storage(root=settings.storage_path)
    engine = WatermarkEngine(
        password_img=settings.wm_pw_img,
        password_wm=settings.wm_pw_wm,
    )

    bot = Bot(
        token=settings.admin_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Уроки уходят ученикам от имени student-бота — у автора нет права
    # писать им первым, а у student-бота оно есть.
    student_bot = Bot(
        token=settings.student_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    dispatcher["storage"] = storage
    dispatcher["broadcaster"] = LessonBroadcaster(
        bot=student_bot,
        engine=engine,
        storage=storage,
        session_factory=database.session_factory,
        rate_interval=settings.send_interval,
        workers=settings.wm_workers,
    )
    dispatcher["tracer"] = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )

    dispatcher.update.outer_middleware(AdminOnlyMiddleware(settings.admin_id_set))
    dispatcher.update.outer_middleware(DbSessionMiddleware(database.session_factory))
    dispatcher.include_router(build_router())

    me = await bot.get_me()
    student_me = await student_bot.get_me()
    logger.info(
        "admin-bot запущен: @%s, раздача через @%s, метка %d бит",
        me.username,
        student_me.username,
        WM_BIT_LENGTH,
    )
    await bot.set_my_commands(_COMMANDS)

    try:
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        await bot.session.close()
        await student_bot.session.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
