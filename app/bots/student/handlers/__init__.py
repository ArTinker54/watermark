"""Роутеры student-бота."""

from __future__ import annotations

from aiogram import Router

from app.bots.student.handlers import chats, common, questions, registration, video


def build_router() -> Router:
    """Порядок важен: ``questions`` ловит любое сообщение и идёт последним,
    а ``video`` должен успеть перехватить видео до него."""
    router = Router(name="student")
    router.include_router(chats.router)
    router.include_router(registration.router)
    router.include_router(common.router)
    router.include_router(video.router)
    router.include_router(questions.router)
    return router


__all__ = ["build_router"]
