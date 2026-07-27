"""Регистрация ученика: оферта -> согласие -> uid.

Регистрация только pull-овая: Telegram не даёт боту написать первым тому, кто
не нажал Start. Поэтому и действующие участники заходят тем же путём — через
/start в этом боте.
"""

from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repo import get_student_by_tg_id, register_student
from app.texts import load_offer
from app.utils import split_text

logger = logging.getLogger(__name__)

router = Router(name="registration")
# Бот живёт и в группе курса (там он проверяет членство), но разговаривать
# должен только в личке: иначе /start вывалил бы текст оферты в общий чат.
router.message.filter(F.chat.type == "private")

CONSENT_CALLBACK = "offer:accept"

_ALLOWED_MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Принимаю", callback_data=CONSENT_CALLBACK)]
        ]
    )


def _welcome(uid_str: str) -> str:
    return (
        f"Вы в списке участников. Ваш номер: <code>{uid_str}</code>\n\n"
        "Уроки будут приходить сюда автоматически — ничего делать не нужно.\n\n"
        "Помните: каждая присланная вам картинка содержит невидимую метку "
        "с вашим номером."
    )


async def _is_group_member(bot: Bot, group_id: int, user_id: int) -> bool | None:
    """``None`` — проверить не удалось (бот не в группе, неверный id и т. п.)."""
    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
    except TelegramBadRequest as exc:
        logger.error("проверка членства в группе %s не удалась: %s", group_id, exc)
        return None
    return member.status in _ALLOWED_MEMBER_STATUSES


async def _send_offer(message: Message, settings: Settings) -> None:
    chunks = split_text(load_offer(settings.offer_path))
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=_consent_keyboard())


@router.message(CommandStart())
async def handle_start(
    message: Message, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    user = message.from_user
    if user is None:  # апдейты из каналов — не наш случай
        return

    student = await get_student_by_tg_id(session, user.id)
    if student is not None and student.has_consent:
        await message.answer(_welcome(student.uid_str))
        return

    if settings.vsa_group_id is not None:
        member = await _is_group_member(bot, settings.vsa_group_id, user.id)
        if member is None:
            await message.answer(
                "Не удалось проверить ваше участие в группе. "
                "Напишите администратору курса."
            )
            return
        if not member:
            await message.answer(
                "Доступ к материалам выдаётся только участникам группы курса. "
                "Если вы уже оплатили участие — напишите администратору."
            )
            return

    await message.answer(
        "Здравствуйте! Прежде чем выдать доступ к материалам, "
        "нужно принять условия."
    )
    await _send_offer(message, settings)


@router.callback_query(F.data == CONSENT_CALLBACK)
async def handle_consent(callback: CallbackQuery, session: AsyncSession) -> None:
    user = callback.from_user
    student = await register_student(
        session,
        tg_user_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    logger.info(
        "оферта принята: uid=%s tg_id=%s (%s)",
        student.uid_str,
        student.tg_user_id,
        student.display_name,
    )

    if isinstance(callback.message, Message):
        # Сообщение могло устареть или быть уже отредактировано — не повод падать.
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(_welcome(student.uid_str))
    await callback.answer("Условия приняты")
