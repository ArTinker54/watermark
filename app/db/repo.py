"""Доступ к данным. Все функции принимают открытую сессию и не коммитят сами,
кроме тех, где это явно указано в docstring."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Delivery,
    DeliveryStatus,
    Lesson,
    LessonImage,
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
) -> Lesson:
    """Создать урок. Коммитит сама.

    ``materialize`` получает уже присвоенный id урока и обязана разложить
    пристинные оригиналы по местам, вернув их описания. Порядок именно такой,
    потому что путь к файлам содержит id, а id известен только после flush;
    зато не нужен ни второй UPDATE, ни временные пути в базе.
    """
    # images=[] инициализирует коллекцию сразу: после flush объект становится
    # persistent, и присваивание в незагруженную связь дёрнуло бы ленивый SELECT
    # вне greenlet-контекста — в async это падение, а не подзагрузка.
    lesson = Lesson(admin_tg_id=admin_tg_id, caption=caption, original_image_path="", images=[])
    session.add(lesson)
    await session.flush()

    images = list(materialize(lesson.id))
    if not images:
        await session.rollback()
        raise ValueError("урок без картинок не имеет смысла: метку некуда вшивать")

    lesson.original_image_path = str(images[0].path)
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
) -> None:
    """Записать доставку. Повторная рассылка того же урока обновляет запись."""
    statement = sqlite_insert(Delivery).values(
        lesson_id=lesson_id,
        student_id=student_id,
        wm_payload=wm_payload,
        status=status.value,
        tg_message_id=tg_message_id,
        error=error,
        delivered_at=utcnow(),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Delivery.lesson_id, Delivery.student_id],
            set_={
                "wm_payload": statement.excluded.wm_payload,
                "status": statement.excluded.status,
                "tg_message_id": statement.excluded.tg_message_id,
                "error": statement.excluded.error,
                "delivered_at": statement.excluded.delivered_at,
            },
        )
    )
    await session.commit()


async def get_delivery(
    session: AsyncSession, *, lesson_id: int, student_id: int
) -> Delivery | None:
    result = await session.execute(
        select(Delivery).where(
            Delivery.lesson_id == lesson_id, Delivery.student_id == student_id
        )
    )
    return result.scalar_one_or_none()


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
    deliveries_sent: int
    deliveries_failed: int
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
    lessons = await scalar(select(func.count()).select_from(Lesson))
    sent = await scalar(
        select(func.count()).select_from(Delivery).where(Delivery.status == DeliveryStatus.SENT)
    )
    failed = await scalar(
        select(func.count()).select_from(Delivery).where(Delivery.status == DeliveryStatus.FAILED)
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
        deliveries_sent=sent,
        deliveries_failed=failed,
        traces_total=traces_total,
        traces_success=traces_success,
        last_lesson_at=last_lesson.scalar_one(),
    )
