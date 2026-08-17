"""Команда самопроверки метки. Вся работа — в :mod:`app.services.selfcheck`."""

from __future__ import annotations

import logging
from html import escape as quote

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import Storage
from app.services.selfcheck import check
from app.watermark import WatermarkEngine

logger = logging.getLogger(__name__)

router = Router(name="selfcheck")


@router.message(Command("selftest"))
async def selftest(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    storage: Storage,
    engine: WatermarkEngine,
) -> None:
    """Проверить, что метка вшивается и читается — на боевом материале."""
    raw = (command.args or "").strip().lstrip("#")
    lesson_id = int(raw) if raw.isdigit() else None

    status = await message.answer("Проверяю метку на выданных копиях…")
    lines = await check(session, storage, engine, lesson_id)
    body = "\n".join(f"• {quote(line)}" for line in lines)
    await status.edit_text(f"<b>Самопроверка метки</b>\n\n{body}")
