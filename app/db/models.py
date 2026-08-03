"""Модель данных: ученики, уроки, доставки, журнал трассировок.

Времена хранятся наивным UTC: SQLite-диалект SQLAlchemy пишет datetime строкой
без смещения, поэтому tz-aware значение всё равно потеряло бы зону при чтении.
Пользуйтесь :func:`utcnow`, а не ``datetime.now()``.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo — формат хранения в этой базе."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Базовый класс декларативных моделей."""


def _enum_column(enum_type: type[enum.Enum]) -> SAEnum:
    """Хранить значения перечисления строками ('active'), а не именами ('ACTIVE')."""
    return SAEnum(
        enum_type,
        native_enum=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class StudentStatus(enum.StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class DeliveryStatus(enum.StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Не отправляли осознанно — например, ученик вышел из группы курса."""


class QuestionStatus(enum.StrEnum):
    NEW = "new"
    ANSWERED = "answered"
    SKIPPED = "skipped"


class Student(Base):
    """Участник курса. Регистрируется только сам, через /start в student-bot."""

    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    uid: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    """Короткий публичный идентификатор. Показывается как 0042 (см. uid_str)."""

    username: Mapped[str | None] = mapped_column(String(64), default=None)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[StudentStatus] = mapped_column(
        _enum_column(StudentStatus), default=StudentStatus.ACTIVE, index=True
    )
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    """Когда принята оферта. NULL означает, что доступ выдавать нельзя."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )

    @property
    def uid_str(self) -> str:
        return f"{self.uid:04d}"

    @property
    def display_name(self) -> str:
        parts = [self.full_name.strip()] if self.full_name.strip() else []
        if self.username:
            parts.append(f"@{self.username}")
        return " ".join(parts) or f"tg:{self.tg_user_id}"

    @property
    def has_consent(self) -> bool:
        return self.consent_at is not None


class Question(Base):
    """Вопрос ученика. Ответ на него выдаётся такой же меченой копией.

    Разбор по запросу — самый ценный контент, и раньше он уходил в общий чат
    без метки. Теперь ответ идёт через бота и метится наравне с уроком.
    """

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str | None] = mapped_column(Text, default=None)
    image_path: Mapped[str | None] = mapped_column(String(512), default=None)
    """Скриншот, приложенный учеником к вопросу."""

    status: Mapped[QuestionStatus] = mapped_column(
        _enum_column(QuestionStatus), default=QuestionStatus.NEW, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    student: Mapped[Student] = relationship(lazy="selectin")

    @property
    def preview(self) -> str:
        if self.text:
            return self.text if len(self.text) <= 300 else self.text[:297] + "…"
        return "(без текста, только картинка)"


class Course(Base):
    """Курс: группа или канал, членство в котором даёт доступ к материалам.

    Курсов может быть несколько. Ученик получает материалы тех, где он состоит:
    подписан на два — придут оба потока, вышел из одного — останется второй.
    Состав аудитории ведёт Telegram, а не мы, поэтому доступ всегда актуален.
    """

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __str__(self) -> str:  # pragma: no cover - для логов
        return f"{self.title} ({self.chat_id})"


class UploadedVideo(Base):
    """Видео, загруженное автором в student-бота и готовое к рассылке.

    Файл никуда не скачивается: хранится только идентификатор, по которому
    Telegram отдаёт его получателям. Поэтому размер ограничен лишь тем, что
    Telegram разрешает загрузить самому автору, а не лимитом Bot API в 20 МБ.

    Идентификатор файла привязан к КОНКРЕТНОМУ боту, поэтому видео принимает
    именно тот бот, который потом будет его раздавать.
    """

    __tablename__ = "uploaded_videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    file_id: Mapped[str] = mapped_column(String(256))
    file_unique_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="video")
    """``video`` или ``document``. Отправлять надо тем же способом, каким
    получили: идентификатор документа метод sendVideo не примет."""

    file_name: Mapped[str | None] = mapped_column(String(256), default=None)
    file_size: Mapped[int | None] = mapped_column(BigInteger, default=None)
    duration: Mapped[int | None] = mapped_column(Integer, default=None)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def summary(self) -> str:
        parts = [self.file_name or "видео"]
        if self.duration:
            parts.append(f"{self.duration // 60}:{self.duration % 60:02d}")
        if self.file_size:
            parts.append(f"{self.file_size / 1024 / 1024:.0f} МБ")
        return " · ".join(parts)


class Lesson(Base):
    """Материал: текст поста и пристинные оригиналы картинок (без метки!).

    Ответ на вопрос — тот же материал, просто со ссылкой на вопрос. Механика
    метки, рассылки и трассировки для них общая.
    """

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger)
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    original_image_path: Mapped[str] = mapped_column(String(512))
    """Главная (первая) картинка урока. Полный список — в ``images``."""

    question_id: Mapped[int | None] = mapped_column(
        ForeignKey("questions.id", ondelete="SET NULL"), default=None, index=True
    )
    """Заполнено, если материал — ответ на вопрос ученика."""

    video_file_id: Mapped[str | None] = mapped_column(String(256), default=None)
    """Идентификатор видео у student-бота. Метки в видео нет — см. README."""

    video_kind: Mapped[str | None] = mapped_column(String(16), default=None)
    """``video`` или ``document`` — каким способом видео отправлять."""

    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), default=None, index=True
    )
    """Кому предназначен материал. NULL — всем зарегистрированным (так вели
    себя все уроки до появления курсов, и старые записи это сохраняют)."""

    course: Mapped[Course | None] = relationship(lazy="selectin")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    question: Mapped[Question | None] = relationship(lazy="selectin")

    @property
    def is_answer(self) -> bool:
        return self.question_id is not None

    @property
    def title(self) -> str:
        return f"Ответ на вопрос №{self.question_id}" if self.is_answer else f"Урок #{self.id}"

    images: Mapped[list[LessonImage]] = relationship(
        back_populates="lesson",
        cascade="all, delete-orphan",
        order_by="LessonImage.position",
        lazy="selectin",
    )
    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )


class LessonImage(Base):
    """Одна пристинная картинка урока.

    Геометрия хранится рядом: при трассировке кроп нормализуют именно к ней,
    и лишний раз открывать файл на каждого кандидата незачем.
    """

    __tablename__ = "lesson_images"
    __table_args__ = (UniqueConstraint("lesson_id", "position", name="uq_lesson_image_position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[str] = mapped_column(String(512))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)

    lesson: Mapped[Lesson] = relationship(back_populates="images")

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


class Delivery(Base):
    """Факт выдачи урока конкретному ученику с конкретной меткой."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("lesson_id", "student_id", name="uq_delivery_lesson_student"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True
    )
    wm_payload: Mapped[str] = mapped_column(String(32))
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum_column(DeliveryStatus), default=DeliveryStatus.SENT, index=True
    )
    error: Mapped[str | None] = mapped_column(String(512), default=None)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    lesson: Mapped[Lesson] = relationship(back_populates="deliveries")
    student: Mapped[Student] = relationship(back_populates="deliveries")


class TraceAttempt(Base):
    """Журнал попыток трассировки — сама по себе доказательная база."""

    __tablename__ = "trace_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger)
    image_path: Mapped[str] = mapped_column(String(512))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    payload_raw: Mapped[str | None] = mapped_column(String(64), default=None)
    lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("lessons.id", ondelete="SET NULL"), default=None
    )
    student_id: Mapped[int | None] = mapped_column(
        ForeignKey("students.id", ondelete="SET NULL"), default=None
    )
    note: Mapped[str | None] = mapped_column(String(512), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
