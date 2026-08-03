"""Сборка student-бота — отдельно от точки входа, чтобы его можно было
поднять и одним процессом вместе с admin-ботом (см. ``app.bots.combined``)."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand

from app.bots.middlewares import DbSessionMiddleware
from app.bots.runtime import BotRuntime, make_bot
from app.bots.student.handlers import build_router
from app.config import Settings
from app.db import Database
from app.services import Storage

COMMANDS = [
    BotCommand(command="start", description="Регистрация и условия"),
    BotCommand(command="id", description="Мой номер участника"),
    BotCommand(command="offer", description="Условия доступа"),
    BotCommand(command="help", description="Справка"),
]


def build_student(
    settings: Settings,
    database: Database,
    *,
    bot: Bot | None = None,
    admin_notifier: Bot,
) -> BotRuntime:
    """``bot`` передают, когда объект уже создан для рассылки, — чтобы не
    держать два подключения к одному и тому же боту.

    ``admin_notifier`` — подключение admin-бота: им уходят уведомления о новых
    вопросах. Своим токеном student-бот автору написать не может, если тот не
    нажимал у него Start.
    """
    owns_session = bot is None
    bot = bot or make_bot(settings.student_bot_token)

    # По той же причине, что и в админке: апдейты одного человека — по очереди.
    # Здесь это защищает вопрос с картинкой и двойное нажатие «Принимаю».
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher["settings"] = settings
    dispatcher["storage"] = Storage(root=settings.storage_path)
    dispatcher["admin_notifier"] = admin_notifier
    dispatcher.update.outer_middleware(DbSessionMiddleware(database.session_factory))
    dispatcher.include_router(build_router())

    return BotRuntime(
        name="student-bot",
        token_env="STUDENT_BOT_TOKEN",
        bot=bot,
        dispatcher=dispatcher,
        commands=COMMANDS,
        owns_session=owns_session,
    )
