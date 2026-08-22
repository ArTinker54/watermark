"""Сборка бота личных сообщений.

Бот необязательный: если ``DM_BOT_TOKEN`` не задан, он просто не поднимается,
а остальные два работают как работали. Сделано так намеренно — новый бот не
должен уметь уронить рассылку материалов.
"""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation

from app.bots.dm.handlers import build_router
from app.bots.middlewares import DbSessionMiddleware
from app.bots.runtime import BotRuntime, make_bot
from app.config import Settings
from app.db import Database


def build_dm(settings: Settings, database: Database) -> BotRuntime | None:
    """Собрать бота личных сообщений. ``None``, если токен не задан."""
    if not settings.dm_bot_token:
        return None

    # Изоляция по той же причине, что и у остальных: человек, который быстро
    # тыкает в несколько кнопок меню, не должен получить ответы вперемешку.
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher["settings"] = settings
    dispatcher.update.outer_middleware(DbSessionMiddleware(database.session_factory))
    dispatcher.include_router(build_router())

    return BotRuntime(
        name="dm-bot",
        token_env="DM_BOT_TOKEN",
        bot=make_bot(settings.dm_bot_token),
        dispatcher=dispatcher,
        # Команд не заводим: у бота одна поверхность — кнопки под сообщением.
        # Меню команд в личной переписке автора выглядело бы чужеродно.
        commands=[],
    )
