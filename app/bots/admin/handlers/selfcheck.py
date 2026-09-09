"""Служебные команды: самопроверка метки и место на томе."""

from __future__ import annotations

import asyncio
import logging
from html import escape as quote

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import Storage
from app.services.cleanup import (
    human,
    purge_marked,
    purge_staging,
    report,
    usage,
)
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


@router.message(Command("cleanup"))
async def cleanup(message: Message, command: CommandObject, storage: Storage) -> None:
    """Место на томе: показать, что занято, и по слову «да» убрать лишнее.

    Без подтверждения команда ничего не удаляет. Удаление здесь безобидное —
    персональные копии собираются заново из оригиналов, — но команда, которая
    стирает файлы по одному слову, обязана сначала показать, что именно.
    """
    before = await asyncio.to_thread(usage, storage)
    lines = ["<b>Место на диске</b>", "", *report(before)]

    if (command.args or "").strip().lower() not in {"да", "do", "yes"}:
        lines += [
            "",
            "Убрать персональные копии и брошенные черновики: "
            "<code>/cleanup да</code>",
            "",
            "Оригиналы уроков, трассировки и вопросы не трогаются: копию можно "
            "собрать заново, а их — неоткуда.",
        ]
        await message.answer("\n".join(lines))
        return

    status = await message.answer("Убираю…")
    marked_bytes, marked_files = await asyncio.to_thread(purge_marked, storage)
    staged_bytes, staged_files = await asyncio.to_thread(purge_staging, storage)
    after = await asyncio.to_thread(usage, storage)

    logger.info(
        "уборка тома: копий %d (%s), черновиков %d (%s)",
        marked_files, human(marked_bytes), staged_files, human(staged_bytes),
    )
    await status.edit_text(
        "\n".join(
            [
                "<b>Убрано</b>",
                "",
                f"персональных копий: {marked_files} — {human(marked_bytes)}",
                f"черновиков: {staged_files} — {human(staged_bytes)}",
                "",
                *report(after),
            ]
        )
    )
