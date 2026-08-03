"""Роутеры admin-бота."""

from __future__ import annotations

from aiogram import Router

from app.bots.admin.handlers import (
    courses,
    drafts,
    lessons,
    questions,
    recipients,
    stats,
    trace,
)


def build_router() -> Router:
    """Порядок важен: сценарии урока и ответа забирают свои картинки раньше,
    чем трассировка подхватит присланное изображение.

    ``drafts`` — последним: он отвечает только на то, что не разобрал ни один
    сценарий (кнопка потерянного черновика, /done и /cancel вне сценария).
    """
    router = Router(name="admin")
    router.include_router(lessons.router)
    router.include_router(questions.router)
    router.include_router(courses.router)
    router.include_router(recipients.router)
    router.include_router(trace.router)
    router.include_router(stats.router)
    router.include_router(drafts.router)
    return router


__all__ = ["build_router"]
