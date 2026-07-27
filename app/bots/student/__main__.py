"""Точка входа student-бота: ``python -m app.bots.student``."""

from __future__ import annotations

import asyncio

from app.bots.runtime import run_bots
from app.bots.student.setup import build_student
from app.config import get_settings
from app.db import create_database
from app.logging_setup import setup_logging


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    database = create_database(settings)
    await database.create_all()

    await run_bots([build_student(settings, database)], database)


if __name__ == "__main__":
    asyncio.run(main())
