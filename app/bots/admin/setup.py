"""Сборка admin-бота.

Уроки уходят ученикам от имени student-бота: у бота автора нет права писать
первым тем, кто не нажимал у него Start. Поэтому сюда передаётся ещё и
подключение student-бота.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bots.admin.handlers import build_router
from app.bots.middlewares import AdminOnlyMiddleware, DbSessionMiddleware
from app.bots.runtime import BotRuntime, make_bot
from app.config import Settings
from app.db import Database
from app.services import LessonBroadcaster, Storage, TraceService
from app.watermark import WatermarkEngine

COMMANDS = [
    BotCommand(command="newlesson", description="Новый урок и рассылка"),
    BotCommand(command="trace", description="Найти источник утечки"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="cancel", description="Прервать сценарий"),
]


def build_admin(settings: Settings, database: Database, *, student_bot: Bot) -> BotRuntime:
    storage = Storage(root=settings.storage_path)
    engine = WatermarkEngine(
        password_img=settings.wm_pw_img,
        password_wm=settings.wm_pw_wm,
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

    return BotRuntime(
        name="admin-bot",
        token_env="ADMIN_BOT_TOKEN",
        bot=make_bot(settings.admin_bot_token),
        dispatcher=dispatcher,
        commands=COMMANDS,
    )
