"""Оба бота одним процессом: ``python -m app.bots.combined``.

Нужно там, где диск нельзя примонтировать сразу к двум сервисам — например
на Railway, где том привязан ровно к одному сервису. База и оригиналы уроков
общие по определению: без оригиналов не работает трассировка, поэтому
растащить боты по разным дискам нельзя.

На VPS с docker-compose предпочтительнее два отдельных процесса: падение
одного бота там не уносит второй.
"""

from __future__ import annotations

import asyncio
import logging

from app.bots.admin.setup import build_admin
from app.bots.runtime import make_bot, run_bots
from app.bots.student.setup import build_student
from app.config import get_settings
from app.db import create_database
from app.logging_setup import setup_logging
from app.watermark import WM_BIT_LENGTH

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.admin_id_set:
        logger.error("ADMIN_IDS пуст: admin-бот не ответит никому")
    logger.info("длина метки: %d бит", WM_BIT_LENGTH)

    database = create_database(settings)
    await database.create_all()

    # По одному объекту Bot на токен: student-бот и принимает /start, и
    # рассылает уроки; admin-бот и общается с автором, и шлёт ему уведомления
    # о вопросах. Вторые подключения к тем же ботам ни к чему.
    student_bot = make_bot(settings.student_bot_token)
    admin_bot = make_bot(settings.admin_bot_token)
    runtimes = [
        build_student(settings, database, bot=student_bot, admin_notifier=admin_bot),
        build_admin(settings, database, student_bot=student_bot, bot=admin_bot),
    ]
    try:
        await run_bots(runtimes, database)
    finally:
        await student_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
