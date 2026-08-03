"""Ответ автора на вопрос ученика.

Ответ — такой же материал, как урок: те же меченые копии, тот же журнал
доставок, та же трассировка. Отличается лишь адресатом (один ученик или все)
и ссылкой на вопрос.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from html import escape as quote
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.admin.handlers.report import format_report
from app.bots.files import ImageRejected, download_image, extract_file_id
from app.config import Settings
from app.db import Course, Question, QuestionStatus, Student
from app.db.repo import (
    get_course,
    get_question,
    list_active_students,
    list_courses,
    list_open_questions,
    set_question_status,
)
from app.services import LessonBroadcaster, LessonSpec, Storage, save_lesson
from app.services.delivery import MEDIA_GROUP_LIMIT
from app.utils import CAPTION_LIMIT, split_html
from app.watermark import image_size

logger = logging.getLogger(__name__)

router = Router(name="questions-admin")

ANSWER_PREFIX = "q:ans"
SKIP_PREFIX = "q:skip"
SEND_NAMED = "ans:send:named"
SEND_ANON = "ans:send:anon"
SEND_CANCEL = "ans:cancel"


class AnswerQuestion(StatesGroup):
    collecting = State()
    confirming = State()


def question_card(question: Question, student: Student) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка вопроса для автора: что спросили, кто и что можно сделать."""
    text = (
        f"<b>Вопрос №{question.id}</b>\n"
        f"От: <b>{student.uid_str}</b> — {quote(student.display_name)}\n\n"
        f"{quote(question.preview)}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Ответить лично {student.uid_str}",
                    callback_data=f"{ANSWER_PREFIX}:{question.id}:personal",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ответить всем ученикам",
                    callback_data=f"{ANSWER_PREFIX}:{question.id}:all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отложить", callback_data=f"{SKIP_PREFIX}:{question.id}"
                )
            ],
        ]
    )
    return text, keyboard


@router.message(Command("questions"))
async def show_open_questions(message: Message, session: AsyncSession) -> None:
    questions = await list_open_questions(session)
    if not questions:
        await message.answer("Вопросов без ответа нет.")
        return

    await message.answer(f"Вопросов без ответа: {len(questions)}")
    for question in questions:
        text, keyboard = question_card(question, question.student)
        # noqa ASYNC240: локальная ФС, микросекунды — поток тут дороже проверки
        if question.image_path and Path(question.image_path).exists():  # noqa: ASYNC240
            await message.answer_photo(
                photo=FSInputFile(question.image_path), caption=text, reply_markup=keyboard
            )
        else:
            await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith(f"{SKIP_PREFIX}:"))
async def skip_question(callback: CallbackQuery, session: AsyncSession) -> None:
    question_id = int(str(callback.data).split(":")[-1])
    question = await get_question(session, question_id)
    if question is not None:
        await set_question_status(session, question, QuestionStatus.SKIPPED)
    await callback.answer("Отложено")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith(f"{ANSWER_PREFIX}:"))
async def start_answer(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, storage: Storage
) -> None:
    _, _, raw_id, audience = str(callback.data).split(":")
    question = await get_question(session, int(raw_id))
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if question is None:
        await callback.message.answer("Вопрос не найден.")
        return

    staging = storage.new_staging_dir()
    await state.set_state(AnswerQuestion.collecting)
    await state.set_data(
        {
            "staging": str(staging),
            "images": [],
            "caption": None,
            "question_id": question.id,
            "audience": audience,
        }
    )
    await callback.message.edit_reply_markup(reply_markup=None)

    who = "лично" if audience == "personal" else "всем ученикам"
    await callback.message.answer(
        f"<b>Ответ на вопрос №{question.id}</b> ({who})\n\n"
        f"<i>{question.preview}</i>\n\n"
        "Пришлите ответ: текст и картинки. Когда закончите — /done.\n"
        "Отмена — /cancel."
    )


@router.message(AnswerQuestion.collecting, F.photo | F.document)
async def collect_answer_image(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    images: list[str] = list(data.get("images", []))
    if len(images) >= MEDIA_GROUP_LIMIT:
        await message.answer(
            f"Больше {MEDIA_GROUP_LIMIT} картинок в один ответ не поместится — "
            "столько Telegram не берёт в один альбом."
        )
        return
    try:
        target = Storage.staged_original(Path(data["staging"]), len(images))
        width, height = await download_image(bot, extract_file_id(message), target)
    except ImageRejected as exc:
        await message.answer(f"Не принято: {exc}")
        return

    images.append(str(target))
    caption = data.get("caption")
    if caption is None and message.caption:
        caption = message.html_text
    await state.update_data(images=images, caption=caption)
    await message.answer(f"Картинка {len(images)} принята ({width}×{height}). Ещё? Или /done.")


@router.message(AnswerQuestion.collecting, Command("done"))
async def finish_answer(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    data = await state.get_data()
    images = [Path(item) for item in data.get("images", [])]
    caption: str | None = data.get("caption")
    if not images and not caption:
        await message.answer("Пустой ответ отправлять некуда. Пришлите текст или картинку.")
        return

    audience = data["audience"]
    recipients = await _recipients(session, data["question_id"], audience)
    await state.set_state(AnswerQuestion.confirming)

    if images:
        await message.answer("<b>Так это увидит ученик:</b>")
        await message.answer_photo(
            photo=FSInputFile(images[0]),
            caption=caption if caption and len(caption) <= CAPTION_LIMIT else None,
        )
    if caption and (not images or len(caption) > CAPTION_LIMIT):
        for chunk in split_html(caption):
            await message.answer(chunk)

    courses = list(await list_courses(session))
    lines = [f"Получателей: {len(recipients)}"]
    if courses or settings.vsa_group_id is not None:
        # И для личного ответа тоже: спросивший мог выйти из курса после вопроса.
        lines.append("Кого уже нет в курсе — пропустим, список будет в отчёте.")
    if any(max(image_size(path)) > settings.lesson_max_side for path in images):
        lines.append(
            f"Длинная сторона будет уменьшена до {settings.lesson_max_side} px: "
            "иначе метка не читается со скриншота чата."
        )
    if audience != "personal" and courses:
        lines.append("\n<b>Кому отправляем?</b> Получат только участники выбранного курса.")
    await message.answer(
        "\n".join(lines), reply_markup=_send_keyboard(audience, courses)
    )


def _send_keyboard(audience: str, courses: Sequence[Course]) -> InlineKeyboardMarkup:
    """Кнопки отправки. Для ответа всем — по кнопке на курс.

    Курс тут обязателен ровно по той же причине, что и у урока: без него
    аудиторией становится единственная старая группа из настроек, и подписчики
    второго курса молча выпадают.
    """
    if audience == "personal":
        # Личный ответ адресован тому, кто спросил, и курса не выбирает: право
        # на ответ даёт членство в любом из его курсов.
        rows = [[InlineKeyboardButton(text="Отправить", callback_data=f"{SEND_ANON}:0")]]
    elif courses:
        # Имя спросившего показываем не всегда: одним важно, что вопрос их,
        # другим неловко. Поэтому выбор здесь, а не в общей настройке.
        rows = [
            [
                InlineKeyboardButton(
                    text=f"С именем: {course.title}",
                    callback_data=f"{SEND_NAMED}:{course.id}",
                )
            ]
            for course in courses
        ]
        rows += [
            [
                InlineKeyboardButton(
                    text=f"Анонимно: {course.title}",
                    callback_data=f"{SEND_ANON}:{course.id}",
                )
            ]
            for course in courses
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="Отправить с именем", callback_data=f"{SEND_NAMED}:0")],
            [InlineKeyboardButton(text="Отправить анонимно", callback_data=f"{SEND_ANON}:0")],
        ]
    rows.append([InlineKeyboardButton(text="Отменить", callback_data=SEND_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _recipients(
    session: AsyncSession, question_id: int, audience: str
) -> list[Student]:
    if audience == "all":
        return list(await list_active_students(session))
    question = await get_question(session, question_id)
    return [question.student] if question is not None else []


@router.callback_query(
    AnswerQuestion.confirming,
    F.data.startswith(f"{SEND_NAMED}:") | F.data.startswith(f"{SEND_ANON}:"),
)
async def send_answer(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    storage: Storage,
    broadcaster: LessonBroadcaster,
    settings: Settings,
) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return

    data = await state.get_data()
    staged = [Path(item) for item in data.get("images", [])]
    question = await get_question(session, data["question_id"])
    if question is None:
        await message.answer("Вопрос потерялся. Начните заново: /questions")
        await state.clear()
        return

    raw_action, raw_course = str(callback.data).rsplit(":", 1)
    course_id = int(raw_course) or None
    course = await get_course(session, course_id) if course_id else None

    recipients = await _recipients(session, question.id, data["audience"])
    caption = _compose(
        question,
        body=data.get("caption"),
        audience=data["audience"],
        with_name=(raw_action == SEND_NAMED),
    )

    lesson = await save_lesson(
        session,
        storage,
        admin_tg_id=callback.from_user.id,
        caption=caption,
        staged=staged,
        max_side=settings.lesson_max_side,
        question_id=question.id,
        course_id=course.id if course else None,
    )
    Storage.drop_staging(Path(data["staging"]))
    await state.clear()

    if not recipients:
        await message.answer(f"Ответ сохранён (материал #{lesson.id}), но получателей нет.")
        return

    # Для личного ответа курса нет: право на него даёт членство в любом курсе.
    # Иначе допуск свалился бы на единственную группу из настроек, и ученику
    # второго курса ответить было бы невозможно в принципе.
    entitle_chats = [item.chat_id for item in await list_courses(session)]
    report = await broadcaster.run(
        LessonSpec.of(lesson, entitle_chats=entitle_chats), recipients
    )

    # Отвеченным помечаем только после реальной доставки. Иначе вопрос ученика,
    # который к этому моменту вышел из группы, исчез бы из /questions, хотя
    # ответа он так и не получил.
    if report.sent:
        await set_question_status(session, question, QuestionStatus.ANSWERED)

    await message.answer(format_report(f"Ответ на вопрос №{question.id} отправлен", report))
    if not report.sent:
        await message.answer(
            f"Вопрос №{question.id} остался открытым — ответ никому не дошёл."
        )


def _compose(question: Question, *, body: str | None, audience: str, with_name: bool) -> str:
    """Собрать текст ответа: заголовок с вопросом плюс текст автора.

    Имя и текст вопроса пишет ученик, поэтому они экранируются: одинокая «<» в
    имени сделала бы разметку битой, и Telegram отверг бы ответ у всех сразу.
    """
    if audience == "personal":
        header = f"<b>Ответ на ваш вопрос №{question.id}</b>"
    elif with_name:
        who = quote(question.student.display_name)
        header = f"<b>Ответ на вопрос №{question.id} от {who}</b>"
    else:
        header = f"<b>Ответ на вопрос №{question.id}</b>"

    parts = [header]
    if question.text:
        parts.append(f"<i>{quote(question.preview)}</i>")
    if body:
        parts.append(body)
    return "\n\n".join(parts)


@router.callback_query(AnswerQuestion.confirming, F.data == SEND_CANCEL)
async def cancel_answer(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _drop(state)
    if isinstance(callback.message, Message):
        await callback.message.answer("Ответ отменён. Вопрос остался без ответа.")


@router.message(
    StateFilter(AnswerQuestion.collecting, AnswerQuestion.confirming), Command("cancel")
)
async def cancel_answer_command(message: Message, state: FSMContext) -> None:
    await _drop(state)
    await message.answer("Ответ отменён. Вопрос остался без ответа.")


@router.message(AnswerQuestion.collecting, F.text, ~F.text.startswith("/"))
async def collect_answer_text(message: Message, state: FSMContext) -> None:
    await state.update_data(caption=message.html_text)
    await message.answer("Текст ответа сохранён. Добавьте картинки или /done.")


async def _drop(state: FSMContext) -> None:
    data = await state.get_data()
    staging = data.get("staging")
    if staging:
        Storage.drop_staging(Path(staging))
    await state.clear()
