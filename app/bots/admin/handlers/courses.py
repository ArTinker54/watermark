"""Управление курсами: какие группы и каналы дают доступ к материалам."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Course
from app.db.repo import add_course, get_course, list_courses, set_course_active

logger = logging.getLogger(__name__)

router = Router(name="courses")

TOGGLE_PREFIX = "course:toggle"

_HELP = (
    "<b>Курсы</b>\n\n"
    "Курс — это группа или канал: кто в нём состоит, тот и получает его материалы.\n"
    "Состоит в двух — получит оба потока.\n\n"
    "Добавить: <code>/addcourse -1001234567890 Метод VSA</code>\n"
    "Узнать id: отправьте <code>/groupid</code> в самой группе или канале.\n\n"
    "Бот рассылки должен быть там администратором, иначе он не сможет "
    "проверять участников."
)


def _keyboard(courses: Sequence[Course]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'Выключить' if course.is_active else 'Включить'} — {course.title}",
                    callback_data=f"{TOGGLE_PREFIX}:{course.id}",
                )
            ]
            for course in courses
        ]
    )


@router.message(Command("courses"))
async def show_courses(message: Message, session: AsyncSession) -> None:
    courses = list(await list_courses(session, only_active=False))
    if not courses:
        await message.answer(f"Курсов пока нет.\n\n{_HELP}")
        return

    lines = ["<b>Курсы</b>", ""]
    for course in courses:
        mark = "✅" if course.is_active else "⏸"
        lines.append(f"{mark} <b>{course.title}</b>\n   <code>{course.chat_id}</code>")
    lines.append("\nВыключенный курс не предлагается при рассылке и не даёт доступа.")
    await message.answer("\n".join(lines), reply_markup=_keyboard(courses))


@router.message(Command("addcourse"))
async def add_new_course(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    parts = (command.args or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        await message.answer(_HELP)
        return

    chat_id, title = int(parts[0]), parts[1].strip()
    course = await add_course(session, title=title, chat_id=chat_id)
    logger.info("курс добавлен: %s", course)
    await message.answer(
        f"<b>Курс добавлен: {course.title}</b>\n<code>{course.chat_id}</code>\n\n"
        "Проверьте, что бот рассылки — администратор в этом чате, иначе он не "
        "сможет определять участников и материалы никому не уйдут."
    )


@router.callback_query(F.data.startswith(f"{TOGGLE_PREFIX}:"))
async def toggle_course(callback: CallbackQuery, session: AsyncSession) -> None:
    course = await get_course(session, int(str(callback.data).split(":")[-1]))
    if course is None:
        await callback.answer("Курс не найден")
        return

    await set_course_active(session, course, not course.is_active)
    await callback.answer("Включён" if course.is_active else "Выключен")

    courses = list(await list_courses(session, only_active=False))
    if isinstance(callback.message, Message):
        lines = ["<b>Курсы</b>", ""]
        for item in courses:
            mark = "✅" if item.is_active else "⏸"
            lines.append(f"{mark} <b>{item.title}</b>\n   <code>{item.chat_id}</code>")
        await callback.message.edit_text("\n".join(lines), reply_markup=_keyboard(courses))
