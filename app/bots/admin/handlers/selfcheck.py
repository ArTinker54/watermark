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
    # «/selftest 143 195» — урок и, если нужно, конкретный участник.
    parts = (command.args or "").replace("#", " ").split()
    numbers = [int(item) for item in parts if item.isdigit()]
    lesson_id = numbers[0] if numbers else None
    uid = numbers[1] if len(numbers) > 1 else None

    status = await message.answer("Проверяю метку на выданных копиях…")
    lines = await check(session, storage, engine, lesson_id, uid)
    body = "\n".join(f"• {quote(line)}" for line in lines)
    await status.edit_text(f"<b>Самопроверка метки</b>\n\n{body}")
