"""Роутеры student-бота."""

from __future__ import annotations

from aiogram import Router

from app.bots.student.handlers import chats, common, registration


def build_router() -> Router:
    """Порядок важен: ловушка на любой текст в ``common`` идёт последней."""
    router = Router(name="student")
    router.include_router(chats.router)
    router.include_router(registration.router)
    router.include_router(common.router)
    return router


__all__ = ["build_router"]
