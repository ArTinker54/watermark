"""Приём вопросов от учеников.

Раньше любое сообщение ученика упиралось в подсказку «уроки приходят
автоматически». Теперь оно становится вопросом: разбор по запросу — самый
ценный материал курса, и уходить он должен через бота, с меткой, а не голой
картинкой в общем чате.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.files import ImageRejected, download_image, extract_file_id
from app.config import Settings
from app.db import Question, Student
from app.db.repo import (
    MAX_OPEN_QUESTIONS,
    count_open_questions,
    create_question,
    get_student_by_tg_id,
)
from app.services import Storage

logger = logging.getLogger(__name__)

router = Router(name="questions")
router.message.filter(F.chat.type == "private")

_NOT_REGISTERED = "Вы ещё не приняли условия. Нажмите /start, чтобы получить доступ."
_TOO_MANY = (
    "У вас уже {count} вопросов без ответа. Дождитесь ответа на них — "
    "так автор точно ничего не потеряет."
)


@router.message(F.text | F.photo | F.document)
async def accept_question(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    storage: Storage,
    bot: Bot,
    admin_notifier: Bot,
) -> None:
    user = message.from_user
    if user is None:
        return

    student = await get_student_by_tg_id(session, user.id)
    if student is None or not student.has_consent:
        await message.answer(_NOT_REGISTERED)
        return

    open_count = await count_open_questions(session, student.id)
    if open_count >= MAX_OPEN_QUESTIONS:
        await message.answer(_TOO_MANY.format(count=open_count))
        return

    image_path = None
    if message.photo or message.document:
        try:
            image_path = storage.question_image()
            await download_image(bot, extract_file_id(message), image_path)
        except ImageRejected as exc:
            await message.answer(f"Картинку не приняли: {exc}")
            return

    text = message.html_text if (message.text or message.caption) else None
    question = await create_question(
        session, student_id=student.id, text=text, image_path=image_path
    )
    logger.info(
        "вопрос №%d от uid=%s (%s)", question.id, student.uid_str, student.display_name
    )

    await message.answer(
        f"Вопрос отправлен автору курса. Ответ придёт сюда же.\n\n"
        f"Номер вопроса: <b>№{question.id}</b>"
    )
    await _notify_admins(admin_notifier, settings, question, student)


async def _notify_admins(
    notifier: Bot, settings: Settings, question: Question, student: Student
) -> None:
    """Толкнуть автору уведомление, иначе вопросы будут копиться незамеченными.

    Уведомление шлёт admin-бот: у student-бота нет права писать автору, если
    тот не нажимал у него Start.
    """
    from app.bots.admin.handlers.questions import question_card

    text, keyboard = question_card(question, student)
    for admin_id in settings.admin_id_set:
        try:
            if question.image_path:
                from aiogram.types import FSInputFile

                await notifier.send_photo(
                    chat_id=admin_id,
                    photo=FSInputFile(question.image_path),
                    caption=text,
                    reply_markup=keyboard,
                )
            else:
                await notifier.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
        except Exception:  # noqa: BLE001 - один недоступный админ не рвёт приём вопроса
            logger.exception("не удалось уведомить админа %s о вопросе %d", admin_id, question.id)
