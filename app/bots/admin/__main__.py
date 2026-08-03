"""Точка входа admin-бота: ``python -m app.bots.admin``.

Процесс держит два подключения к Telegram: своим токеном общается с автором,
а токеном student-бота раздаёт уроки ученикам. Отдельный канал связи между
ботами не нужен — база и хранилище общие.
"""

from __future__ import annotations

import asyncio
import logging

from app.bots.admin.setup import build_admin
from app.bots.runtime import make_bot, migrate_group_to_course, run_bots
from app.config import get_settings
from app.db import create_database
from app.logging_setup import setup_logging
from app.watermark import WM_BIT_LENGTH

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.admin_id_set:
        logger.error("ADMIN_IDS пуст: бот запустится, но не ответит никому")
    logger.info("длина метки: %d бит", WM_BIT_LENGTH)

    database = create_database(settings)
    await database.create_all()
    await migrate_group_to_course(database, settings)

    student_bot = make_bot(settings.student_bot_token)
    runtime = build_admin(settings, database, student_bot=student_bot)
    try:
        await run_bots([runtime], database)
    finally:
        await student_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
