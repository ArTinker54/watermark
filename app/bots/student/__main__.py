"""Точка входа student-бота: ``python -m app.bots.student``."""

from __future__ import annotations

import asyncio

from app.bots.runtime import make_bot, run_bots
from app.bots.student.setup import build_student
from app.config import get_settings
from app.db import create_database
from app.logging_setup import setup_logging


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    database = create_database(settings)
    await database.create_all()

    # Подключение admin-бота нужно только на отправку: им уходят уведомления
    # о новых вопросах. Опрашивает его отдельный процесс.
    notifier = make_bot(settings.admin_bot_token)
    runtime = build_student(settings, database, admin_notifier=notifier)
    try:
        await run_bots([runtime], database)
    finally:
        await notifier.session.close()


if __name__ == "__main__":
    asyncio.run(main())
