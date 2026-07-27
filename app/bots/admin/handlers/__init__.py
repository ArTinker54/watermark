"""Роутеры admin-бота."""

from __future__ import annotations

from aiogram import Router

from app.bots.admin.handlers import lessons, stats, trace


def build_router() -> Router:
    """Порядок важен: сценарий урока забирает свои картинки раньше трассировки."""
    router = Router(name="admin")
    router.include_router(lessons.router)
    router.include_router(trace.router)
    router.include_router(stats.router)
    return router


__all__ = ["build_router"]
