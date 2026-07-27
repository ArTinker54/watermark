"""Общие middleware обоих ботов."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия БД на апдейт, доступна хендлеру как аргумент ``session``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            return await handler(event, data)


class AdminOnlyMiddleware(BaseMiddleware):
    """Whitelist admin-бота: всё, что пришло не от автора, молча выбрасывается.

    Именно молча — чужой не должен по ответу бота понять, что он вообще существует.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self._admin_ids = admin_ids
        if not admin_ids:
            logger.warning("ADMIN_IDS пуст — admin-бот не ответит никому")

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self._admin_ids:
            if user is not None:
                logger.warning("отказано в доступе к admin-боту: user_id=%s", user.id)
            return None
        return await handler(event, data)
