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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repo import (
    UidAssignmentError,
    UidExhaustedError,
    get_student_by_tg_id,
    list_courses,
    register_student,
)
from app.services.membership import in_any_course
from app.texts import load_offer
from app.utils import split_html

logger = logging.getLogger(__name__)

router = Router(name="registration")
# Бот живёт и в группе курса (там он проверяет членство), но разговаривать
# должен только в личке: иначе /start вывалил бы текст оферты в общий чат.
router.message.filter(F.chat.type == "private")

CONSENT_CALLBACK = "offer:accept"



def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Принимаю", callback_data=CONSENT_CALLBACK)]
        ]
    )


def _welcome(uid_str: str) -> str:
    """Текст после принятия условий.

    Про метку здесь намеренно ничего нет: по решению заказчика упоминание
    индивидуальной метки убрано из всех текстов для учеников. Не возвращайте
    его «для полноты» — это осознанный выбор, а не упущение. Заодно не пишите
    обратного: утверждать, что меток нет, нельзя, они есть.
    """
    return (
        f"Вы в списке участников. Ваш номер: <code>{uid_str}</code>\n\n"
        "Уроки будут приходить сюда автоматически — ничего делать не нужно.\n\n"
        "Материалы защищены авторским правом и предназначены только для вас."
    )


async def _send_offer(message: Message, settings: Settings) -> None:
    chunks = split_html(load_offer(settings.offer_path))
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

    # Курсов может быть несколько: доступ даёт членство в любом из них, а какие
    # именно материалы придут — решается уже при рассылке.
    courses = await list_courses(session)
    chat_ids = [course.chat_id for course in courses]
    if not chat_ids and settings.vsa_group_id is not None:
        chat_ids = [settings.vsa_group_id]

    if chat_ids:
        member = await in_any_course(bot, chat_ids, user.id)
        if member is None:
            await message.answer(
                "Не удалось проверить ваше участие. Напишите администратору курса."
            )
            return
        if not member:
            await message.answer(
                "Доступ к материалам выдаётся только участникам курса. "
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
    try:
        student = await register_student(
            session,
            tg_user_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )
    except UidExhaustedError:
        logger.error("свободные номера кончились: tg_id=%s", user.id)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Не получилось выдать доступ — закончились свободные номера. "
                "Напишите администратору курса."
            )
        return
    except (UidAssignmentError, SQLAlchemyError):
        # Молчание тут — худший исход: человек нажал «Принимаю», ничего не
        # произошло, и он не знает, зарегистрирован он или нет.
        logger.exception("регистрация не удалась: tg_id=%s", user.id)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Не получилось записать вас с первого раза. "
                "Нажмите «Принимаю» ещё раз — обычно со второй попытки проходит."
            )
        return

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
