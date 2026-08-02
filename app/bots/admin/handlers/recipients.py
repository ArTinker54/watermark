"""Сводка по получателям: кто получит следующий материал и кто получил прошлые."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import DeliveryStatus
from app.db.repo import (
    get_lesson,
    list_active_students,
    list_lesson_deliveries,
    list_lesson_summaries,
)
from app.services import LessonBroadcaster
from app.utils import format_dt

logger = logging.getLogger(__name__)

router = Router(name="recipients")

#: Сколько материалов показывать в сводке.
_RECENT = 10
#: Сколько имён показывать в списке, чтобы не упереться в лимит сообщения.
_NAMES = 40


@router.message(Command("recipients"))
async def show_recipients(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    broadcaster: LessonBroadcaster,
) -> None:
    """Сколько человек получит материал прямо сейчас.

    Число живое: членство в группе проверяется запросами к Telegram, а не
    берётся из базы. Именно оно определяет, кому уйдёт следующий урок.
    """
    students = list(await list_active_students(session))
    if not students:
        await message.answer("Ни один ученик ещё не принял условия.")
        return

    if settings.vsa_group_id is None:
        await message.answer(
            f"<b>Получателей: {len(students)}</b>\n\n"
            "Проверка членства в группе выключена — материалы получат все, "
            "кто принял условия."
        )
        return

    status = await message.answer(f"Проверяю {len(students)} учеников…")
    inside, outside = await broadcaster.split_by_membership(students)

    lines = [
        f"<b>Получателей: {len(inside)}</b>",
        f"Принято условий: {len(students)}",
    ]
    if outside:
        lines.append(f"\n<b>Не получат — нет в группе курса ({len(outside)}):</b>")
        lines.extend(f"• {s.uid_str} — {s.display_name}" for s in outside[:_NAMES])
        if len(outside) > _NAMES:
            lines.append(f"…и ещё {len(outside) - _NAMES}")
    if outside and not inside and len(students) > 1:
        lines.append(
            "\n⚠️ Не прошёл никто. Похоже на сбой настройки: проверьте, "
            "что бот раздачи всё ещё администратор группы."
        )
    await status.edit_text("\n".join(lines))


@router.message(Command("lessons"))
async def show_lessons(message: Message, session: AsyncSession) -> None:
    """Сколько человек получило каждый из последних материалов."""
    summaries = await list_lesson_summaries(session, limit=_RECENT)
    if not summaries:
        await message.answer("Материалов пока нет.")
        return

    lines = [f"<b>Последние материалы ({len(summaries)})</b>", ""]
    for item in summaries:
        counts = [f"получили {item.sent}"]
        if item.skipped:
            counts.append(f"пропущено {item.skipped}")
        if item.failed:
            counts.append(f"ошибок {item.failed}")
        lines.append(f"<b>{item.lesson.title}</b> · {format_dt(item.lesson.created_at)}")
        lines.append("   " + " · ".join(counts))
    lines.append("\nПодробности: /lesson &lt;номер&gt;")
    await message.answer("\n".join(lines))


@router.message(Command("lesson"))
async def show_lesson(message: Message, command: CommandObject, session: AsyncSession) -> None:
    """Поимённо: кто получил материал, кого пропустили, у кого сорвалось."""
    raw = (command.args or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Укажите номер материала: <code>/lesson 9</code>")
        return

    lesson = await get_lesson(session, int(raw))
    if lesson is None:
        await message.answer(f"Материала #{raw} нет.")
        return

    rows = await list_lesson_deliveries(session, lesson.id)
    if not rows:
        await message.answer(
            f"<b>{lesson.title}</b> · {format_dt(lesson.created_at)}\n\nЕщё никому не выдавался."
        )
        return

    groups: dict[DeliveryStatus, list[str]] = {status: [] for status in DeliveryStatus}
    for delivery, student in rows:
        groups[delivery.status].append(f"• {student.uid_str} — {student.display_name}")

    lines = [
        f"<b>{lesson.title}</b> · {format_dt(lesson.created_at)}",
        f"Получили: <b>{len(groups[DeliveryStatus.SENT])}</b>",
    ]
    titles = {
        DeliveryStatus.SENT: "Получили",
        DeliveryStatus.SKIPPED: "Пропущены — нет в группе курса",
        DeliveryStatus.FAILED: "Не доставлено",
    }
    for status, title in titles.items():
        names = groups[status]
        if not names:
            continue
        lines.append(f"\n<b>{title} ({len(names)}):</b>")
        lines.extend(names[:_NAMES])
        if len(names) > _NAMES:
            lines.append(f"…и ещё {len(names) - _NAMES}")
    await message.answer("\n".join(lines))
