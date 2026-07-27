"""Трассировка «крысы»: по утёкшей картинке — uid и личность ученика.

Порядок: найти на присланном изображении область урока (template match по
пристинным оригиналам), выровнять кроп к геометрии оригинала, извлечь метку.

Ключевая деталь — тройная проверка результата. Извлечение из чуть неточного
кропа умеет возвращать ЧАСТИЧНО верную строку: например ``V|0042|00401`` вместо
``V|0042|00001``. Формат она проходит, а урок называет чужой. Поэтому одного
совпадения с регуляркой мало, и результат принимается, только если:

1. строка совпала с форматом метки;
2. ``lesson_id`` из метки равен id урока, оригинал которого мы нашли на картинке;
3. ``uid`` принадлежит существующему ученику.

Дополнительно сверяется факт доставки: этому ученику этот урок правда выдавался.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Lesson, LessonImage, Student
from app.db.repo import get_delivery, get_student_by_uid, list_lessons, log_trace_attempt
from app.watermark import (
    CoarseMatch,
    Payload,
    WatermarkEngine,
    load_bgr,
    locate_async,
    locate_coarse_async,
)
from app.watermark.engine import BgrImage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TraceHit:
    """Метка прочитана и прошла все проверки."""

    payload: Payload
    lesson: Lesson
    student: Student | None
    confidence: float
    box: tuple[int, int, int, int]
    delivery_confirmed: bool
    """True — в журнале есть запись, что этому ученику этот урок правда выдавался."""


@dataclass(frozen=True, slots=True)
class TraceMiss:
    """Метка не прочитана. Причина — честная, без догадок."""

    reason: str
    checked: int
    best_confidence: float | None = None
    best_lesson_id: int | None = None


TraceResult = TraceHit | TraceMiss


@dataclass(frozen=True, slots=True)
class _Candidate:
    lesson: Lesson
    image: LessonImage
    coarse: CoarseMatch


class TraceService:
    """Поиск автора утечки по присланной картинке."""

    def __init__(
        self,
        *,
        engine: WatermarkEngine,
        session_factory: async_sessionmaker[AsyncSession],
        min_confidence: float,
        max_candidates: int = 3,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory
        self._min_confidence = min_confidence
        self._max_candidates = max_candidates

    async def trace(self, suspect_path: Path, *, admin_tg_id: int) -> TraceResult:
        suspect = await asyncio.to_thread(load_bgr, suspect_path)

        async with self._session_factory() as session:
            lessons = list(await list_lessons(session))

        pairs = [(lesson, image) for lesson in lessons for image in lesson.images]
        if not pairs:
            result: TraceResult = TraceMiss(reason="в базе пока нет ни одного урока", checked=0)
            await self._log(admin_tg_id, suspect_path, result)
            return result

        candidates = await self._rank(suspect, pairs)
        best = candidates[0] if candidates else None

        for candidate in candidates[: self._max_candidates]:
            hit = await self._try_candidate(suspect, candidate)
            if hit is not None:
                await self._log(admin_tg_id, suspect_path, hit)
                return hit

        result = TraceMiss(
            reason=self._explain(best),
            checked=len(pairs),
            best_confidence=best.coarse.confidence if best else None,
            best_lesson_id=best.lesson.id if best else None,
        )
        await self._log(admin_tg_id, suspect_path, result)
        return result

    async def _rank(
        self, suspect: BgrImage, pairs: list[tuple[Lesson, LessonImage]]
    ) -> list[_Candidate]:
        """Дешёвый проход по всем оригиналам, отсортированный по похожести."""

        async def scan(lesson: Lesson, image: LessonImage) -> _Candidate | None:
            path = Path(image.path)
            # noqa ASYNC240: обращение к локальной ФС, микросекунды —
            # отдельный поток обошёлся бы дороже самой проверки.
            if not path.exists():  # noqa: ASYNC240
                logger.warning("оригинал урока %d пропал с диска: %s", lesson.id, path)
                return None
            coarse = await locate_coarse_async(suspect, path)
            if coarse is None:
                return None
            return _Candidate(lesson=lesson, image=image, coarse=coarse)

        scanned = await asyncio.gather(*(scan(lesson, image) for lesson, image in pairs))
        found = [item for item in scanned if item is not None]
        found.sort(key=lambda item: item.coarse.confidence, reverse=True)
        return [item for item in found if item.coarse.confidence >= self._min_confidence]

    async def _try_candidate(self, suspect: BgrImage, candidate: _Candidate) -> TraceHit | None:
        original = Path(candidate.image.path)
        match = await locate_async(suspect, original, hint=candidate.coarse)
        if match is None:
            return None

        size = candidate.image.size
        payload = await asyncio.to_thread(self._engine.extract, match.crop, size)
        if payload is None:
            # Запасной заход: утечка могла быть не скриншотом, а самим файлом,
            # который просто пережали — тогда кроп не нужен вовсе.
            payload = await asyncio.to_thread(self._engine.extract, suspect, size)
        if payload is None:
            return None

        if payload.lesson_id != candidate.lesson.id:
            logger.info(
                "метка прочиталась частично: урок %d, а в метке %d — отбрасываем",
                candidate.lesson.id,
                payload.lesson_id,
            )
            return None

        async with self._session_factory() as session:
            student = await get_student_by_uid(session, payload.uid)
            confirmed = False
            if student is not None:
                delivery = await get_delivery(
                    session, lesson_id=candidate.lesson.id, student_id=student.id
                )
                confirmed = delivery is not None and delivery.wm_payload == payload.encode()

        return TraceHit(
            payload=payload,
            lesson=candidate.lesson,
            student=student,
            confidence=match.confidence,
            box=match.box,
            delivery_confirmed=confirmed,
        )

    def _explain(self, best: _Candidate | None) -> str:
        if best is None:
            return (
                "не удалось сопоставить картинку ни с одним уроком — "
                "возможно, это не наш материал или он сильно обрезан"
            )
        return (
            f"урок найден (совпадение {best.coarse.confidence:.0%}), но метка не читается — "
            "скорее всего, картинку слишком сильно пережали или обрезали"
        )

    async def _log(self, admin_tg_id: int, image_path: Path, result: TraceResult) -> None:
        async with self._session_factory() as session:
            if isinstance(result, TraceHit):
                await log_trace_attempt(
                    session,
                    admin_tg_id=admin_tg_id,
                    image_path=image_path,
                    success=True,
                    confidence=result.confidence,
                    payload_raw=result.payload.encode(),
                    lesson_id=result.lesson.id,
                    student_id=result.student.id if result.student else None,
                    note=None if result.delivery_confirmed else "нет записи о доставке",
                )
            else:
                await log_trace_attempt(
                    session,
                    admin_tg_id=admin_tg_id,
                    image_path=image_path,
                    success=False,
                    confidence=result.best_confidence,
                    lesson_id=result.best_lesson_id,
                    note=result.reason[:500],
                )
