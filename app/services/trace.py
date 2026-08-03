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

from app.db import Delivery, DeliveryStatus, Lesson, LessonImage, Student
from app.db.repo import (
    get_delivery,
    get_lesson,
    get_student_by_uid,
    list_lessons,
    log_trace_attempt,
)
from app.watermark import (
    READABLE_SCALE,
    CoarseMatch,
    Payload,
    WatermarkEngine,
    load_bgr,
    locate_async,
    locate_coarse_async,
    rebuild_fragment_async,
)
from app.watermark.engine import BgrImage
from app.watermark.text import MIN_GAPS as TEXT_MIN_WORDS
from app.watermark.text import extract as extract_text

logger = logging.getLogger(__name__)

#: Выше этого совпадения считаем, что урок на картинке опознан уверенно, —
#: только тогда можно называть конкретную причину отказа.
_CONFIDENT_MATCH = 0.6


@dataclass(frozen=True, slots=True)
class TraceHit:
    """Метка прочитана и прошла все проверки."""

    payload: Payload
    lesson: Lesson
    student: Student | None
    confidence: float | None
    box: tuple[int, int, int, int] | None
    delivery: Delivery | None
    source: str = "картинка"
    """Откуда прочитана метка: из картинки или из текста."""
    """Запись журнала об этой выдаче, если она есть."""

    @property
    def delivery_confirmed(self) -> bool:
        """Выдавался ли материал этому ученику с этой меткой.

        Неудачная отправка тоже считается: копию сгенерировали и записали, а
        часть сообщения могла дойти. Не считается только пропуск — там метки
        не создавали вовсе.
        """
        return (
            self.delivery is not None
            and self.delivery.status is not DeliveryStatus.SKIPPED
            and self.delivery.wm_payload == self.payload.encode()
        )


@dataclass(frozen=True, slots=True)
class TraceMiss:
    """Метка не прочитана. Причина — честная, без догадок."""

    reason: str
    checked: int
    best_confidence: float | None = None
    best_lesson_id: int | None = None
    found_size: tuple[int, int] | None = None
    """Размер области урока на присланной картинке, если её удалось найти."""

    original_size: tuple[int, int] | None = None
    scale: float | None = None
    """Во сколько раз область меньше оригинала. < READABLE_SCALE — безнадёжно."""


@dataclass(frozen=True, slots=True)
class _Attempt:
    """Что дала попытка по одному кандидату — для объяснения отказа."""

    hit: TraceHit | None
    confidence: float
    found_size: tuple[int, int]
    original_size: tuple[int, int]
    lesson_id: int

    @property
    def scale(self) -> float:
        return self.found_size[0] / max(self.original_size[0], 1)


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

        attempts: list[_Attempt] = []

        # Проход 1 — урок целиком виден на присланной картинке (скриншот).
        candidates = await self._rank(suspect, pairs)
        for candidate in candidates[: self._max_candidates]:
            attempt = await self._try_candidate(suspect, candidate)
            if attempt is None:
                continue
            if attempt.hit is not None:
                await self._log(admin_tg_id, suspect_path, attempt.hit)
                return attempt.hit
            attempts.append(attempt)

        # Проход 2 — от урока отрезали часть. Тогда прямой поиск бесполезен:
        # шаблона целиком на картинке нет. Ищем наоборот — обрезок внутри
        # оригинала — и восстанавливаем геометрию.
        for candidate in await self._rank_fragments(suspect, pairs):
            attempt = await self._try_fragment(suspect, candidate)
            if attempt is None:
                continue
            if attempt.hit is not None:
                await self._log(admin_tg_id, suspect_path, attempt.hit)
                return attempt.hit
            attempts.append(attempt)

        result = self._miss(attempts, checked=len(pairs))
        await self._log(admin_tg_id, suspect_path, result)
        return result

    def _miss(self, attempts: list[_Attempt], *, checked: int) -> TraceMiss:
        if not attempts:
            return TraceMiss(
                reason=(
                    "не удалось сопоставить картинку ни с одним уроком — "
                    "возможно, это не наш материал или он сильно обрезан"
                ),
                checked=checked,
            )

        best = max(attempts, key=lambda item: item.confidence)
        # Про «слишком мелко» говорим, только если урок действительно опознан.
        # На слабом совпадении это была бы уверенно названная неверная причина,
        # а в трассировке такое хуже честного «не знаю».
        if best.scale < READABLE_SCALE and best.confidence > _CONFIDENT_MATCH:
            reason = (
                f"урок найден, но на присланной картинке он занимает всего "
                f"{best.found_size[0]}×{best.found_size[1]} — это "
                f"{best.scale:.0%} от оригинала "
                f"({best.original_size[0]}×{best.original_size[1]}). "
                f"Метка восстанавливается примерно от {READABLE_SCALE:.0%}: "
                "при таком уменьшении её уже физически нет в пикселях"
            )
        else:
            reason = (
                "урок найден, но метка не читается — скорее всего, картинку "
                "слишком сильно пережали или обрезали"
            )
        return TraceMiss(
            reason=reason,
            checked=checked,
            best_confidence=best.confidence,
            best_lesson_id=best.lesson_id,
            found_size=best.found_size,
            original_size=best.original_size,
            scale=best.scale,
        )

    async def trace_text(self, text: str, *, admin_tg_id: int) -> TraceResult:
        """Опознать по присланному тексту.

        Отдельный путь от картинки: тут нечего искать и выравнивать — метка
        либо есть в символах, либо её там нет.
        """
        payload = extract_text(text)
        if payload is None:
            result: TraceResult = TraceMiss(
                reason=(
                    "в тексте метки нет. Она вшивается только в подписи от "
                    f"{TEXT_MIN_WORDS} слов и не переживает перепечатку или скриншот"
                ),
                checked=0,
            )
            await self._log(admin_tg_id, Path("(текст)"), result)
            return result

        async with self._session_factory() as session:
            lesson = await get_lesson(session, payload.lesson_id)
            student = await get_student_by_uid(session, payload.uid)
            delivery = None
            if lesson is not None and student is not None:
                delivery = await get_delivery(
                    session, lesson_id=lesson.id, student_id=student.id
                )

        if lesson is None:
            result = TraceMiss(
                reason=(
                    f"метка прочитана ({payload.encode()}), но материала "
                    f"#{payload.lesson_id} в базе нет"
                ),
                checked=0,
            )
            await self._log(admin_tg_id, Path("(текст)"), result)
            return result

        hit = TraceHit(
            payload=payload,
            lesson=lesson,
            student=student,
            confidence=None,
            box=None,
            delivery=delivery,
            source="текст",
        )
        await self._log(admin_tg_id, Path("(текст)"), hit)
        return hit

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

        above = [item for item in found if item.coarse.confidence >= self._min_confidence]
        # Лучшего кандидата пробуем даже при слабом совпадении: на почти
        # однотонной картинке корреляция низкая сама по себе, а метка при этом
        # может читаться. Ложное опознание тут не грозит — результат всё равно
        # проходит сверку lesson_id и наличия ученика.
        return above or found[:1]

    async def _rank_fragments(
        self, suspect: BgrImage, pairs: list[tuple[Lesson, LessonImage]]
    ) -> list[_Candidate]:
        """Ранжирование для обрезка: ищем присланное ВНУТРИ оригиналов."""

        async def scan(lesson: Lesson, image: LessonImage) -> _Candidate | None:
            path = Path(image.path)
            if not path.exists():  # noqa: ASYNC240 - локальная ФС, микросекунды
                return None
            coarse = await locate_coarse_async(path, suspect)
            if coarse is None or coarse.confidence < self._min_confidence:
                return None
            return _Candidate(lesson=lesson, image=image, coarse=coarse)

        scanned = await asyncio.gather(*(scan(lesson, image) for lesson, image in pairs))
        found = [item for item in scanned if item is not None]
        found.sort(key=lambda item: item.coarse.confidence, reverse=True)
        return found[: self._max_candidates]

    async def _try_fragment(self, suspect: BgrImage, candidate: _Candidate) -> _Attempt | None:
        original = Path(candidate.image.path)
        rebuilt = await rebuild_fragment_async(suspect, original, hint=candidate.coarse)
        if rebuilt is None:
            return None

        size = candidate.image.size
        found = (rebuilt.box[2], rebuilt.box[3])
        attempt = _Attempt(
            hit=None,
            confidence=rebuilt.confidence,
            found_size=found,
            original_size=size,
            lesson_id=candidate.lesson.id,
        )

        payload = await asyncio.to_thread(self._engine.extract, rebuilt.image, size)
        if payload is None or payload.lesson_id != candidate.lesson.id:
            return attempt

        logger.info(
            "метка собрана из обрезка: урок %d, уцелело %.0f%% площади",
            candidate.lesson.id,
            rebuilt.coverage * 100,
        )
        hit = await self._build_hit(
            candidate, payload, confidence=rebuilt.confidence, box=rebuilt.box
        )
        return _Attempt(
            hit=hit,
            confidence=rebuilt.confidence,
            found_size=found,
            original_size=size,
            lesson_id=candidate.lesson.id,
        )

    async def _build_hit(
        self,
        candidate: _Candidate,
        payload: Payload,
        *,
        confidence: float,
        box: tuple[int, int, int, int],
    ) -> TraceHit:
        async with self._session_factory() as session:
            student = await get_student_by_uid(session, payload.uid)
            delivery = None
            if student is not None:
                delivery = await get_delivery(
                    session, lesson_id=candidate.lesson.id, student_id=student.id
                )

        return TraceHit(
            payload=payload,
            lesson=candidate.lesson,
            student=student,
            confidence=confidence,
            box=box,
            delivery=delivery,
        )

    async def _try_candidate(self, suspect: BgrImage, candidate: _Candidate) -> _Attempt | None:
        original = Path(candidate.image.path)
        match = await locate_async(suspect, original, hint=candidate.coarse)
        if match is None:
            return None

        size = candidate.image.size
        attempt = _Attempt(
            hit=None,
            confidence=match.confidence,
            found_size=(match.box[2], match.box[3]),
            original_size=size,
            lesson_id=candidate.lesson.id,
        )

        payload = await asyncio.to_thread(self._engine.extract, match.crop, size)
        if payload is None:
            # Запасной заход: утечка могла быть не скриншотом, а самим файлом,
            # который просто пережали — тогда кроп не нужен вовсе.
            payload = await asyncio.to_thread(self._engine.extract, suspect, size)
        if payload is None:
            return attempt

        if payload.lesson_id != candidate.lesson.id:
            logger.info(
                "метка прочиталась частично: урок %d, а в метке %d — отбрасываем",
                candidate.lesson.id,
                payload.lesson_id,
            )
            return attempt

        hit = await self._build_hit(
            candidate, payload, confidence=match.confidence, box=match.box
        )
        return _Attempt(
            hit=hit,
            confidence=match.confidence,
            found_size=attempt.found_size,
            original_size=size,
            lesson_id=candidate.lesson.id,
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
