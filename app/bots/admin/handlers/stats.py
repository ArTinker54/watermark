"""Справка и статистика admin-бота."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import collect_stats
from app.utils import format_dt
from app.watermark import WM_BIT_LENGTH

router = Router(name="stats")

_HELP = (
    "<b>Панель автора</b>\n\n"
    "/newlesson — собрать урок и разослать персональные копии\n"
    "/questions — вопросы учеников без ответа\n"
    "/courses — курсы: какие группы и каналы дают доступ\n"
    "/recipients — кто что получит, разбивка по курсам\n"
    "/course &lt;номер&gt; — поимённо: кто в этой аудитории\n"
    "/lessons — материалы и сколько человек получило каждый\n"
    "/lesson &lt;номер&gt; — поимённо по одному материалу\n"
    "/trace — определить, кто слил присланную картинку\n"
    "/stats — цифры по базе\n"
    "/cancel — прервать текущий сценарий\n\n"
    "Вопросы приходят сюда сами, как только ученик их задаст.\n"
    "Картинку или скриншот можно прислать просто так — бот начнёт трассировку."
)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("stats"))
async def handle_stats(message: Message, session: AsyncSession) -> None:
    stats = await collect_stats(session)
    await message.answer(
        "<b>Статистика</b>\n\n"
        f"Учеников: {stats.students_total} "
        f"(получают материалы: {stats.students_active})\n"
        f"Уроков: {stats.lessons}\n"
        f"Ответов на вопросы: {stats.answers}"
        + (f" (без ответа: {stats.questions_open})" if stats.questions_open else "")
        + "\n"
        f"Доставок: {stats.deliveries_sent} успешных"
        + (f", {stats.deliveries_failed} с ошибкой" if stats.deliveries_failed else "")
        + (
            f", {stats.deliveries_skipped} пропущено (нет в группе)"
            if stats.deliveries_skipped
            else ""
        )
        + "\n"
        f"Трассировок: {stats.traces_total} "
        f"(метка найдена в {stats.traces_success})\n"
        f"Последний урок: {format_dt(stats.last_lesson_at)}\n\n"
        f"<i>Длина метки: {WM_BIT_LENGTH} бит</i>"
    )
