"""Запуск ботов: сборка, проверка токена, опрос, корректное закрытие.

Оба бота живут в одном репозитории и умеют запускаться как двумя процессами
(docker-compose), так и одним (``app.bots.combined``). Общий код здесь, чтобы
три точки входа не расходились в мелочах вроде закрытия сессий.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from app.config import Settings
from app.db import Database
from app.db.repo import ensure_default_course

logger = logging.getLogger(__name__)


def make_bot(token: str) -> Bot:
    """Бот с HTML-разметкой по умолчанию — так сохраняется формат поста автора."""
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@dataclass(slots=True)
class BotRuntime:
    """Всё, что нужно, чтобы запустить одного бота."""

    name: str
    token_env: str
    """Имя переменной окружения — чтобы точно сказать, какой токен отвергнут."""

    bot: Bot
    dispatcher: Dispatcher
    commands: list[BotCommand] = field(default_factory=list)
    owns_session: bool = True
    """False, если этот Bot создан кем-то другим и закрывать его не наше дело."""


async def _prepare(runtime: BotRuntime) -> None:
    try:
        me = await runtime.bot.get_me()
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил %s — проверьте .env", runtime.token_env)
        raise SystemExit(1) from None
    logger.info("%s запущен: @%s", runtime.name, me.username)
    if runtime.commands:
        await runtime.bot.set_my_commands(runtime.commands)


async def migrate_group_to_course(database: Database, settings: Settings) -> None:
    """Перенести единственную группу из настроек в курсы.

    До появления курсов группа была одна и жила в ``VSA_GROUP_ID``. Чтобы
    обновление не потребовало ручных действий, она заводится курсом сама — но
    только если курсов ещё нет вовсе.
    """
    if settings.vsa_group_id is None:
        return
    async with database.session_factory() as session:
        await ensure_default_course(session, settings.vsa_group_id, "Курс")


async def run_bots(runtimes: Sequence[BotRuntime], database: Database) -> None:
    """Опрашивать всех до первой остановки, затем свернуться целиком.

    Сигналы обрабатывает только первый диспетчер: если бы их слушали оба,
    второй перетёр бы обработчик первого и SIGTERM останавливал бы одного.
    Когда первый завершился — остальные снимаются здесь.
    """
    bots = [runtime.bot for runtime in runtimes if runtime.owns_session]
    try:
        for runtime in runtimes:
            await _prepare(runtime)

        tasks = [
            asyncio.create_task(
                runtime.dispatcher.start_polling(
                    runtime.bot,
                    handle_signals=(index == 0),
                    allowed_updates=runtime.dispatcher.resolve_used_update_types(),
                ),
                name=runtime.name,
            )
            for index, runtime in enumerate(runtimes)
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # пробросить настоящую причину остановки
    finally:
        # Закрывать надо и при падении на старте, иначе aiohttp ругается
        # на незакрытую сессию поверх настоящей причины ошибки.
        for bot in bots:
            await bot.session.close()
        await database.dispose()
