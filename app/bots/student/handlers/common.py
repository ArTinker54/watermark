"""Справочные команды ученика."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repo import get_student_by_tg_id
from app.texts import load_offer
from app.utils import split_text

router = Router(name="common")

_NOT_REGISTERED = "Вы ещё не приняли условия. Нажмите /start, чтобы получить доступ."

_HELP = (
    "<b>Что умеет этот бот</b>\n\n"
    "/start — регистрация и повторный показ условий\n"
    "/id — ваш персональный номер участника\n"
    "/offer — условия доступа\n"
    "/help — эта справка\n\n"
    "Уроки приходят автоматически, запрашивать их не нужно."
)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("id"))
async def handle_id(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    student = await get_student_by_tg_id(session, message.from_user.id)
    if student is None or not student.has_consent:
        await message.answer(_NOT_REGISTERED)
        return
    await message.answer(
        f"Ваш номер участника: <code>{student.uid_str}</code>\n\n"
        "Он вшит невидимой меткой в каждую выданную вам картинку."
    )


@router.message(Command("offer"))
async def handle_offer(message: Message, settings: Settings) -> None:
    for chunk in split_text(load_offer(settings.offer_path)):
        await message.answer(chunk)


@router.message(F.text)
async def handle_anything_else(message: Message, session: AsyncSession) -> None:
    """Ученику ничего не нужно писать — подсказываем, а не молчим."""
    if message.from_user is None:
        return
    student = await get_student_by_tg_id(session, message.from_user.id)
    if student is None or not student.has_consent:
        await message.answer(_NOT_REGISTERED)
        return
    await message.answer(
        "Уроки приходят сюда автоматически. Справка — /help."
    )
