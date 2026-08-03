"""Доступ к данным. Все функции принимают открытую сессию и не коммитят сами,
кроме тех, где это явно указано в docstring."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Delivery,
    DeliveryStatus,
    Lesson,
    LessonImage,
    Question,
    QuestionStatus,
    Student,
    StudentStatus,
    TraceAttempt,
    utcnow,
)
from app.watermark.payload import MAX_UID

logger = logging.getLogger(__name__)

#: Сколько раз повторить присвоение uid при гонке двух одновременных /start.
_UID_RETRIES = 5


class UidExhaustedError(RuntimeError):
    """Свободные uid кончились — формат метки не вмещает больше учеников."""


# --- Ученики -------------------------------------------------------------------


async def get_student_by_tg_id(session: AsyncSession, tg_user_id: int) -> Student | None:
    result = await session.execute(select(Student).where(Student.tg_user_id == tg_user_id))
    return result.scalar_one_or_none()


async def get_student_by_uid(session: AsyncSession, uid: int) -> Student | None:
    result = await session.execute(select(Student).where(Student.uid == uid))
    return result.scalar_one_or_none()


async def list_active_students(session: AsyncSession) -> Sequence[Student]:
    result = await session.execute(
        select(Student)
        .where(Student.status == StudentStatus.ACTIVE, Student.consent_at.is_not(None))
        .order_by(Student.uid)
    )
    return result.scalars().all()


async def _next_uid(session: AsyncSession) -> int:
    result = await session.execute(select(func.max(Student.uid)))
    current: int | None = result.scalar_one()
    uid = (current or 0) + 1
    if uid > MAX_UID:
        raise UidExhaustedError(f"uid > {MAX_UID}: формат метки исчерпан")
    return uid


async def register_student(
    session: AsyncSession,
    *,
    tg_user_id: int,
    username: str | None,
    full_name: str,
) -> Student:
    """Зарегистрировать ученика с принятой офертой. Коммитит сама.

    Если ученик уже есть — обновляет профиль и проставляет ``consent_at``,
    когда согласие ещё не было записано. uid при этом не меняется никогда:
    он вшит в уже разосланные картинки.
    """
    existing = await get_student_by_tg_id(session, tg_user_id)
    if existing is not None:
        existing.username = username
        existing.full_name = full_name
        if existing.consent_at is None:
            existing.consent_at = utcnow()
        if existing.status is StudentStatus.BLOCKED:
            existing.status = StudentStatus.ACTIVE
        await session.commit()
        return existing

    for attempt in range(_UID_RETRIES):
        student = Student(
            tg_user_id=tg_user_id,
            uid=await _next_uid(session),
            username=username,
            full_name=full_name,
            status=StudentStatus.ACTIVE,
            consent_at=utcnow(),
        )
        session.add(student)
        try:
            await session.commit()
        except IntegrityError:
            # Гонка одновременных /start: uid или tg_user_id уже заняты.
            await session.rollback()
            duplicate = await get_student_by_tg_id(session, tg_user_id)
            if duplicate is not None:
                return duplicate
            logger.warning("конфликт uid, попытка %d", attempt + 1)
            continue
        return student

    raise RuntimeError("не удалось присвоить uid за отведённое число попыток")


async def set_student_status(
    session: AsyncSession, student: Student, status: StudentStatus
) -> None:
    """Пометить ученика активным/заблокированным. Коммитит сама."""
    student.status = status
    await session.commit()


# --- Уроки ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageSpec:
    """Пристинная картинка урока, уже лежащая в хранилище."""

    path: Path
    width: int
    height: int


async def create_lesson(
    session: AsyncSession,
    *,
    admin_tg_id: int,
    caption: str | None,
    materialize: Callable[[int], Sequence[ImageSpec]],
    question_id: int | None = None,
) -> Lesson:
    """Создать урок. Коммитит сама.

    ``materialize`` получает уже присвоенный id урока и обязана разложить
    пристинные оригиналы по местам, вернув их описания. Порядок именно такой,
    потому что путь к файлам содержит id, а id известен только после flush;
    зато не нужен ни второй UPDATE, ни временные пути в базе.

    Картинок может не быть вовсе: объявление из одного текста — тоже материал,
    и метку теперь несёт сам текст.
    """
    # images=[] инициализирует коллекцию сразу: после flush объект становится
    # persistent, и присваивание в незагруженную связь дёрнуло бы ленивый SELECT
    # вне greenlet-контекста — в async это падение, а не подзагрузка.
    lesson = Lesson(
        admin_tg_id=admin_tg_id,
        caption=caption,
        original_image_path="",
        question_id=question_id,
        images=[],
    )
    session.add(lesson)
    await session.flush()

    images = list(materialize(lesson.id))
    lesson.original_image_path = str(images[0].path) if images else ""
    session.add_all(
        [
            LessonImage(
                lesson_id=lesson.id,
                position=position,
                path=str(spec.path),
                width=spec.width,
                height=spec.height,
            )
            for position, spec in enumerate(images)
        ]
    )
    await session.commit()
    await session.refresh(lesson, attribute_names=["images"])
    return lesson


async def get_lesson(session: AsyncSession, lesson_id: int) -> Lesson | None:
    result = await session.execute(
        select(Lesson).where(Lesson.id == lesson_id).options(selectinload(Lesson.images))
    )
    return result.scalar_one_or_none()


async def list_lessons(session: AsyncSession, *, limit: int | None = None) -> Sequence[Lesson]:
    """Уроки от новых к старым — в этом порядке их и стоит перебирать при трассировке."""
    query = select(Lesson).options(selectinload(Lesson.images)).order_by(Lesson.id.desc())
    if limit is not None:
        query = query.limit(limit)
    result = await session.execute(query)
    return result.scalars().all()


# --- Доставки ------------------------------------------------------------------


async def record_delivery(
    session: AsyncSession,
    *,
    lesson_id: int,
    student_id: int,
    wm_payload: str,
    status: DeliveryStatus,
    tg_message_id: int | None = None,
    error: str | None = None,
    overwrite: bool = True,
) -> None:
    """Записать доставку. Повторная рассылка того же урока обновляет запись.

    ``overwrite=False`` оставляет уже существующую запись нетронутой. Так пишут
    пропуски: если материал ученику когда-то реально выдавали, эта запись —
    доказательство выдачи, и затирать её более поздним «нет в группе» нельзя.
    Меченая копия к тому моменту уже у него на руках.
    """
    statement = sqlite_insert(Delivery).values(
        lesson_id=lesson_id,
        student_id=student_id,
        wm_payload=wm_payload,
        status=status.value,
        tg_message_id=tg_message_id,
        error=error,
        delivered_at=utcnow(),
    )
    index = [Delivery.lesson_id, Delivery.student_id]
    if overwrite:
        statement = statement.on_conflict_do_update(
            index_elements=index,
            set_={
                "wm_payload": statement.excluded.wm_payload,
                "status": statement.excluded.status,
                "tg_message_id": statement.excluded.tg_message_id,
                "error": statement.excluded.error,
                "delivered_at": statement.excluded.delivered_at,
            },
        )
    else:
        statement = statement.on_conflict_do_nothing(index_elements=index)
    await session.execute(statement)
    await session.commit()


@dataclass(frozen=True, slots=True)
class LessonSummary:
    """Материал и что с ним стало при рассылке."""

    lesson: Lesson
    sent: int
    skipped: int
    failed: int

    @property
    def recipients(self) -> int:
        return self.sent


def _count_status(status: DeliveryStatus) -> Any:
    return func.sum(case((Delivery.status == status, 1), else_=0))


async def list_lesson_summaries(
    session: AsyncSession, *, limit: int = 10
) -> Sequence[LessonSummary]:
    """Последние материалы со счётчиками доставок — одним запросом.

    LEFT JOIN, а не INNER: материал без единой доставки тоже надо показать,
    иначе несостоявшаяся рассылка просто исчезнет из сводки.
    """
    query = (
        select(
            Lesson,
            _count_status(DeliveryStatus.SENT).label("sent"),
            _count_status(DeliveryStatus.SKIPPED).label("skipped"),
            _count_status(DeliveryStatus.FAILED).label("failed"),
        )
        .outerjoin(Delivery, Delivery.lesson_id == Lesson.id)
        .options(selectinload(Lesson.images), selectinload(Lesson.question))
        .group_by(Lesson.id)
        .order_by(Lesson.id.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    return [
        LessonSummary(lesson=row[0], sent=int(row[1] or 0), skipped=int(row[2] or 0),
                      failed=int(row[3] or 0))
        for row in result.all()
    ]


async def list_lesson_deliveries(
    session: AsyncSession, lesson_id: int
) -> Sequence[tuple[Delivery, Student]]:
    """Кому и с каким исходом выдавался материал."""
    result = await session.execute(
        select(Delivery, Student)
        .join(Student, Student.id == Delivery.student_id)
        .where(Delivery.lesson_id == lesson_id)
        .order_by(Student.uid)
    )
    return [(row[0], row[1]) for row in result.all()]


async def get_delivery(
    session: AsyncSession, *, lesson_id: int, student_id: int
) -> Delivery | None:
    result = await session.execute(
        select(Delivery).where(
            Delivery.lesson_id == lesson_id, Delivery.student_id == student_id
        )
    )
    return result.scalar_one_or_none()


# --- Вопросы -------------------------------------------------------------------

#: Сколько неотвеченных вопросов можно накопить одному ученику.
MAX_OPEN_QUESTIONS = 5


async def count_open_questions(session: AsyncSession, student_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Question)
        .where(Question.student_id == student_id, Question.status == QuestionStatus.NEW)
    )
    return int(result.scalar_one() or 0)


async def create_question(
    session: AsyncSession,
    *,
    student_id: int,
    text: str | None,
    image_path: Path | None,
) -> Question:
    """Записать вопрос. Коммитит сама."""
    question = Question(
        student_id=student_id,
        text=text,
        image_path=str(image_path) if image_path else None,
    )
    session.add(question)
    await session.commit()
    await session.refresh(question, attribute_names=["student"])
    return question


async def get_question(session: AsyncSession, question_id: int) -> Question | None:
    result = await session.execute(
        select(Question).where(Question.id == question_id).options(selectinload(Question.student))
    )
    return result.scalar_one_or_none()


async def list_open_questions(session: AsyncSession, *, limit: int = 20) -> Sequence[Question]:
    result = await session.execute(
        select(Question)
        .where(Question.status == QuestionStatus.NEW)
        .options(selectinload(Question.student))
        .order_by(Question.id)
        .limit(limit)
    )
    return result.scalars().all()


async def set_question_status(
    session: AsyncSession, question: Question, status: QuestionStatus
) -> None:
    """Отметить вопрос отвеченным или отложенным. Коммитит сама."""
    question.status = status
    if status is QuestionStatus.ANSWERED:
        question.answered_at = utcnow()
    await session.commit()


# --- Журнал трассировок --------------------------------------------------------


async def log_trace_attempt(
    session: AsyncSession,
    *,
    admin_tg_id: int,
    image_path: Path,
    success: bool,
    confidence: float | None = None,
    payload_raw: str | None = None,
    lesson_id: int | None = None,
    student_id: int | None = None,
    note: str | None = None,
) -> None:
    """Зафиксировать попытку трассировки. Коммитит сама."""
    session.add(
        TraceAttempt(
            admin_tg_id=admin_tg_id,
            image_path=str(image_path),
            success=success,
            confidence=confidence,
            payload_raw=payload_raw,
            lesson_id=lesson_id,
            student_id=student_id,
            note=note,
        )
    )
    await session.commit()


# --- Статистика ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stats:
    students_total: int
    students_active: int
    lessons: int
    answers: int
    deliveries_sent: int
    deliveries_failed: int
    deliveries_skipped: int
    questions_open: int
    traces_total: int
    traces_success: int
    last_lesson_at: datetime | None


async def collect_stats(session: AsyncSession) -> Stats:
    async def scalar(query) -> int:  # type: ignore[no-untyped-def]
        result = await session.execute(query)
        return int(result.scalar_one() or 0)

    students_total = await scalar(select(func.count()).select_from(Student))
    students_active = await scalar(
        select(func.count())
        .select_from(Student)
        .where(Student.status == StudentStatus.ACTIVE, Student.consent_at.is_not(None))
    )
    lessons = await scalar(
        select(func.count()).select_from(Lesson).where(Lesson.question_id.is_(None))
    )
    answers = await scalar(
        select(func.count()).select_from(Lesson).where(Lesson.question_id.is_not(None))
    )
    questions_open = await scalar(
        select(func.count()).select_from(Question).where(Question.status == QuestionStatus.NEW)
    )
    sent = await scalar(
        select(func.count()).select_from(Delivery).where(Delivery.status == DeliveryStatus.SENT)
    )
    failed = await scalar(
        select(func.count()).select_from(Delivery).where(Delivery.status == DeliveryStatus.FAILED)
    )
    skipped = await scalar(
        select(func.count()).select_from(Delivery).where(Delivery.status == DeliveryStatus.SKIPPED)
    )
    traces_total = await scalar(select(func.count()).select_from(TraceAttempt))
    traces_success = await scalar(
        select(func.count()).select_from(TraceAttempt).where(TraceAttempt.success.is_(True))
    )
    last_lesson = await session.execute(select(func.max(Lesson.created_at)))

    return Stats(
        students_total=students_total,
        students_active=students_active,
        lessons=lessons,
        answers=answers,
        deliveries_sent=sent,
        deliveries_failed=failed,
        deliveries_skipped=skipped,
        questions_open=questions_open,
        traces_total=traces_total,
        traces_success=traces_success,
        last_lesson_at=last_lesson.scalar_one(),
    )
