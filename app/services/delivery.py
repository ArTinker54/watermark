"""Веерная рассылка урока: персональная меченая копия каждому ученику.

Картинки уходят как photo — Telegram пережмёт их в JPEG, и метка это переживает
(проверено на цепочке jpeg80 -> jpeg75 -> скрин 0.5 -> jpeg90).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InputMediaPhoto
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import DeliveryStatus, Lesson, Student, StudentStatus
from app.db.repo import record_delivery, set_student_status
from app.services.storage import Storage
from app.watermark import Payload, WatermarkEngine, WatermarkError, embed_async

logger = logging.getLogger(__name__)

#: Лимиты Telegram на длину подписи к фото и обычного сообщения.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
#: Сколько раз повторить отправку при 429/5xx.
SEND_RETRIES = 3

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LessonSpec:
    """Снимок урока для рассылки — чтобы не таскать ORM-объекты по задачам."""

    lesson_id: int
    caption: str | None
    images: tuple[Path, ...]

    @classmethod
    def of(cls, lesson: Lesson) -> LessonSpec:
        return cls(
            lesson_id=lesson.id,
            caption=lesson.caption,
            images=tuple(Path(image.path) for image in lesson.images),
        )


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    uid: int
    name: str
    ok: bool
    error: str | None = None


@dataclass(slots=True)
class BroadcastReport:
    total: int
    outcomes: list[DeliveryOutcome] = field(default_factory=list)

    @property
    def sent(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def failed(self) -> list[DeliveryOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]


class RateLimiter:
    """Простая очередь: не чаще одного действия в ``interval`` секунд.

    На полсотни учеников это формальность, но лимит Telegram (~30 msg/s) никуда
    не денется при росте базы, и очередь должна быть заложена сразу.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


def _split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    return [text[start : start + limit] for start in range(0, len(text), limit)] or [text]


class LessonBroadcaster:
    """Готовит меченые копии и раздаёт их через токен student-bot."""

    def __init__(
        self,
        *,
        bot: Bot,
        engine: WatermarkEngine,
        storage: Storage,
        session_factory: async_sessionmaker[AsyncSession],
        rate_interval: float,
        workers: int,
        protect_content: bool = True,
    ) -> None:
        self._bot = bot
        self._engine = engine
        self._storage = storage
        self._session_factory = session_factory
        self._limiter = RateLimiter(rate_interval)
        self._embed_slots = asyncio.Semaphore(workers)
        self._protect = protect_content

    async def run(
        self,
        lesson: LessonSpec,
        students: Sequence[Student],
        *,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> BroadcastReport:
        report = BroadcastReport(total=len(students))
        done = 0

        # Ученики обрабатываются параллельно: пока одна копия метится (CPU),
        # другая уже уходит в сеть. Отправку сериализует RateLimiter.
        async def worker(student: Student) -> None:
            nonlocal done
            outcome = await self._deliver(lesson, student)
            report.outcomes.append(outcome)
            done += 1
            if on_progress is not None:
                await on_progress(done, report.total)

        await asyncio.gather(*(worker(student) for student in students))
        logger.info(
            "рассылка урока %d завершена: %d/%d",
            lesson.lesson_id,
            report.sent,
            report.total,
        )
        return report

    async def _deliver(self, lesson: LessonSpec, student: Student) -> DeliveryOutcome:
        payload = Payload(uid=student.uid, lesson_id=lesson.lesson_id)
        try:
            marked = await self._prepare(lesson, student)
        except (WatermarkError, OSError, ValueError) as exc:
            return await self._fail(lesson, student, payload, f"метка не встроена: {exc}")

        try:
            message_id = await self._send(student.tg_user_id, lesson.caption, marked)
        except TelegramForbiddenError:
            await self._block(student)
            return await self._fail(lesson, student, payload, "ученик заблокировал бота")
        except Exception as exc:  # noqa: BLE001 - одна осечка не должна рвать рассылку
            logger.exception("не удалось отправить урок ученику %s", student.uid_str)
            return await self._fail(lesson, student, payload, str(exc))

        async with self._session_factory() as session:
            await record_delivery(
                session,
                lesson_id=lesson.lesson_id,
                student_id=student.id,
                wm_payload=payload.encode(),
                status=DeliveryStatus.SENT,
                tg_message_id=message_id,
            )
        logger.info(
            "урок %d выдан uid=%s payload=%s",
            lesson.lesson_id,
            student.uid_str,
            payload.encode(),
        )
        return DeliveryOutcome(uid=student.uid, name=student.display_name, ok=True)

    async def _prepare(self, lesson: LessonSpec, student: Student) -> list[Path]:
        """Сгенерировать (или переиспользовать) персональные копии."""
        payload = Payload(uid=student.uid, lesson_id=lesson.lesson_id)
        result: list[Path] = []
        for position, original in enumerate(lesson.images):
            target = self._storage.marked(lesson.lesson_id, student.uid, position)
            if not target.exists():
                async with self._embed_slots:
                    await embed_async(self._engine, original, target, payload)
            result.append(target)
        return result

    async def _send(self, chat_id: int, caption: str | None, images: Sequence[Path]) -> int | None:
        """Отправить пост. Длинный текст уезжает отдельным сообщением."""
        inline_caption = caption if caption and len(caption) <= CAPTION_LIMIT else None

        if len(images) == 1:
            message = await self._call(
                self._bot.send_photo,
                chat_id=chat_id,
                photo=FSInputFile(images[0]),
                caption=inline_caption,
                protect_content=self._protect,
            )
            message_id: int | None = message.message_id
        else:
            media = [
                InputMediaPhoto(
                    media=FSInputFile(path),
                    caption=inline_caption if index == 0 else None,
                )
                for index, path in enumerate(images)
            ]
            sent = await self._call(
                self._bot.send_media_group,
                chat_id=chat_id,
                media=media,
                protect_content=self._protect,
            )
            message_id = sent[0].message_id if sent else None

        if caption and inline_caption is None:
            for chunk in _split_text(caption):
                await self._call(
                    self._bot.send_message,
                    chat_id=chat_id,
                    text=chunk,
                    protect_content=self._protect,
                )

        return message_id

    async def _call(self, method: Callable[..., Awaitable[T]], **kwargs: Any) -> T:
        """Вызов Telegram API под общей очередью, с повтором на 429/5xx."""
        last: Exception | None = None
        for attempt in range(SEND_RETRIES):
            await self._limiter.acquire()
            try:
                return await method(**kwargs)
            except TelegramRetryAfter as exc:
                last = exc
                logger.warning("Telegram просит подождать %s c", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramServerError as exc:
                last = exc
                await asyncio.sleep(2 * (attempt + 1))
        raise last if last is not None else RuntimeError("отправка не удалась")

    async def _block(self, student: Student) -> None:
        async with self._session_factory() as session:
            merged = await session.merge(student)
            await set_student_status(session, merged, StudentStatus.BLOCKED)

    async def _fail(
        self, lesson: LessonSpec, student: Student, payload: Payload, error: str
    ) -> DeliveryOutcome:
        async with self._session_factory() as session:
            await record_delivery(
                session,
                lesson_id=lesson.lesson_id,
                student_id=student.id,
                wm_payload=payload.encode(),
                status=DeliveryStatus.FAILED,
                error=error[:500],
            )
        logger.warning("урок %d не выдан uid=%s: %s", lesson.lesson_id, student.uid_str, error)
        return DeliveryOutcome(
            uid=student.uid, name=student.display_name, ok=False, error=error
        )
