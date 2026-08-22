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
from app.bots.dm.setup import build_dm
from app.bots.runtime import make_bot, migrate_group_to_course, run_bots
from app.bots.student.setup import build_student
from app.config import Settings, get_settings
from app.db import Database, create_database
from app.logging_setup import setup_logging
from app.services import Storage
from app.services.selfcheck import check
from app.watermark import WM_BIT_LENGTH, WatermarkEngine

logger = logging.getLogger(__name__)


async def selfcheck(settings: Settings, database: Database) -> None:
    """Проверить метку на боевых копиях и написать результат в логи.

    Включается переменной SELFCHECK_LESSON. Нужно потому, что в контейнер на
    Railway не зайти: SSH туда не отвечает, а том с оригиналами и выданными
    копиями существует только внутри. Логи при этом читаются снаружи — значит
    через них можно и спросить, и получить ответ.
    """
    if settings.selfcheck_lesson is None:
        return
    engine = WatermarkEngine(
        password_img=settings.wm_pw_img, password_wm=settings.wm_pw_wm
    )
    storage = Storage(root=settings.storage_path)
    async with database.session_factory() as session:
        try:
            lines = await check(
                session,
                storage,
                engine,
                settings.selfcheck_lesson,
                settings.selfcheck_uid,
            )
        except Exception:
            logger.exception("самопроверка метки сорвалась")
            return
    for line in lines:
        logger.info("САМОПРОВЕРКА: %s", line)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.admin_id_set:
        logger.error("ADMIN_IDS пуст: admin-бот не ответит никому")
    logger.info("длина метки: %d бит", WM_BIT_LENGTH)

    database = create_database(settings)
    await database.create_all()
    await migrate_group_to_course(database, settings)
    await selfcheck(settings, database)

    # По одному объекту Bot на токен: student-бот и принимает /start, и
    # рассылает уроки; admin-бот и общается с автором, и шлёт ему уведомления
    # о вопросах. Вторые подключения к тем же ботам ни к чему.
    student_bot = make_bot(settings.student_bot_token)
    admin_bot = make_bot(settings.admin_bot_token)
    runtimes = [
        build_student(settings, database, bot=student_bot, admin_notifier=admin_bot),
        build_admin(settings, database, student_bot=student_bot, bot=admin_bot),
    ]
    # Бот личных сообщений добавляется, только если задан его токен: без него
    # ничего не ломается, а рассылка материалов от него не зависит.
    dm = build_dm(settings, database)
    if dm is not None:
        runtimes.append(dm)
    else:
        logger.info("DM_BOT_TOKEN не задан — бот личных сообщений не запускается")
    try:
        await run_bots(runtimes, database)
    finally:
        await student_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
