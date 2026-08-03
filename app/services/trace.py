"""Трассировка «крысы»: по утёкшей картинке — uid и личность ученика.

Порядок: найти на присланном изображении область урока (template match по
пристинным оригиналам), выровнять кроп к геометрии оригинала, прочитать метку.

Ключевая деталь — чем именно проверяется прочитанное. Проверять формат строки
недостаточно и опасно: в метке ``V|uid|lesson`` нет ни одного избыточного бита,
поэтому одиночный сбой извлечения даёт другую формально правильную строку — и,
что хуже всего, чаще всего меняет именно ``uid``, оставляя номер урока верным.
Сверка урока такие случаи пропускает целиком, а дальше находится реальный
ученик, у которого в журнале записана ровно эта строка. Итог — уверенное
обвинение невиновного.

Поэтому избыточность берётся из журнала: по найденному уроку известно, какие
именно копии выдавались. Биты не округляются в строку, а сравниваются со всеми
выданными метками (см. ``app.watermark.verify``). Человек называется, только
если одна копия совпала точно и заметно оторвалась от остальных; иначе честный
ответ — «метка повреждена», со списком возможных кандидатов и без имени.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Delivery, DeliveryStatus, Lesson, LessonImage, Student
from app.db.repo import (
    get_delivery,
    get_lesson,
    get_student_by_uid,
    list_issued_payloads,
    list_lessons,
    log_trace_attempt,
)
from app.watermark import (
    MIN_MARGIN,
    READABLE_SCALE,
    CoarseMatch,
    Payload,
    SoftRead,
    Verdict,
    WatermarkEngine,
    decode,
    load_bgr,
    locate_async,
    locate_coarse_async,
    match,
    near_misses,
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

    payload_raw: str | None = None
    """Что прочиталось до сверки. Хранится всегда — это часть доказательной базы."""


@dataclass(frozen=True, slots=True)
class _Attempt:
    """Что дала попытка по одному кандидату — для объяснения отказа."""

    hit: TraceHit | None
    confidence: float
    found_size: tuple[int, int]
    original_size: tuple[int, int]
    lesson_id: int
    damaged: str | None = None
    """Метка прочиталась, но назвать человека нельзя — здесь сказано почему."""

    raw: str | None = None
    """Сырая строка после округления битов. В журнал попадает всегда."""

    @property
    def scale(self) -> float:
        return self.found_size[0] / max(self.original_size[0], 1)


@dataclass(frozen=True, slots=True)
class _Reading:
    """Результат чтения метки с одной картинки."""

    payload: Payload | None = None
    raw: str | None = None
    damaged: str | None = None


TraceResult = TraceHit | TraceMiss


@dataclass(frozen=True, slots=True)
class _Candidate:
    lesson: Lesson
    image: LessonImage
    coarse: CoarseMatch


def _damaged_note(read: SoftRead, issued: Sequence[str], verdict: Verdict) -> str:
    """Объяснение, почему метка есть, а имени не будет.

    Кандидаты показываются намеренно: автору полезно знать круг, но между
    «вот эти шестеро» и «это он» — принципиальная разница, и стирать её нельзя.
    """
    uids = [
        f"{parsed.uid:04d}"
        for parsed in (Payload.parse(item) for item in near_misses(read, issued))
        if parsed is not None
    ]
    who = ", ".join(uids) if uids else "сузить не удалось"

    if verdict.mismatched:
        head = (
            f"метка повреждена: из {len(issued)} выданных копий ни одна не совпала "
            f"точно, ближайшая разошлась на {verdict.mismatched} бит"
        )
    else:
        head = (
            "метка совпала с выданной копией, но соседняя выданная метка почти "
            f"так же похожа на прочитанное (отрыв {verdict.margin:.2f} при пороге "
            f"{MIN_MARGIN}) — одного сбитого бита хватило бы, чтобы поменять ответ"
        )
    return f"{head}. Назвать участника нельзя. Кандидаты: {who}"


def _unknown_student(payload: Payload) -> str:
    return (
        f"метка прочитана ({payload.encode()}), но ученика с номером "
        f"{payload.uid:04d} в базе нет"
    )


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

        # Если метка вообще прочиталась, но не прошла сверку — это куда более
        # содержательный ответ, чем «не читается», и он важнее по существу:
        # автор должен видеть, что след есть, но назвать по нему нельзя.
        damaged = next((item for item in attempts if item.damaged), None)
        if damaged is not None:
            return TraceMiss(
                reason=damaged.damaged or "",
                checked=checked,
                best_confidence=damaged.confidence,
                best_lesson_id=damaged.lesson_id,
                found_size=damaged.found_size,
                original_size=damaged.original_size,
                scale=damaged.scale,
                payload_raw=damaged.raw,
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
            payload_raw=best.raw,
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

    async def _identify(
        self, image: BgrImage, size: tuple[int, int], lesson: Lesson
    ) -> _Reading:
        """Прочитать метку и сверить её с копиями, выданными по этому уроку.

        Здесь и стоит настоящая защита от одиночного сбоя: вместо разбора
        округлённой строки прочитанные биты сравниваются со списком выданных
        меток, и человек называется только при уверенной победе одной из них.
        """
        read = await asyncio.to_thread(self._engine.extract_soft, image, size)
        if read is None:
            return _Reading()

        raw = decode(read)
        async with self._session_factory() as session:
            issued = await list_issued_payloads(session, lesson.id)

        verdict = match(read, issued) if issued else None
        if verdict is None:
            # Сверять не с чем: по уроку не выдано ни одной копии (обычно это
            # означает, что рассылка ещё не проходила). Остаётся старый путь —
            # округлить биты и разобрать формат. Он слабее, поэтому опирается
            # хотя бы на совпадение номера урока.
            payload = Payload.parse(raw) if raw else None
            if payload is None or payload.lesson_id != lesson.id:
                return _Reading(raw=raw)
            logger.info(
                "урок %d ещё никому не выдавался — метку сверить не с чем", lesson.id
            )
            return _Reading(payload=payload, raw=raw)

        if verdict.trustworthy:
            return _Reading(payload=Payload.parse(verdict.payload), raw=raw)

        if not verdict.plausible:
            # Прочитанное не похоже на метку вовсе — говорить о повреждении
            # нечестно. Пусть отказ объяснят по размеру и пережатию.
            return _Reading(raw=raw)

        logger.info(
            "метка урока %d прочиталась ненадёжно: расхождений %d, отрыв %.2f, "
            "шаткий бит %.2f",
            lesson.id,
            verdict.mismatched,
            verdict.margin,
            verdict.weakest_bit,
        )
        return _Reading(raw=raw, damaged=_damaged_note(read, issued, verdict))

    async def _try_fragment(self, suspect: BgrImage, candidate: _Candidate) -> _Attempt | None:
        original = Path(candidate.image.path)
        rebuilt = await rebuild_fragment_async(suspect, original, hint=candidate.coarse)
        if rebuilt is None:
            return None

        size = candidate.image.size
        attempt = _Attempt(
            hit=None,
            confidence=rebuilt.confidence,
            found_size=(rebuilt.box[2], rebuilt.box[3]),
            original_size=size,
            lesson_id=candidate.lesson.id,
        )

        reading = await self._identify(rebuilt.image, size, candidate.lesson)
        if reading.payload is None:
            return replace(attempt, damaged=reading.damaged, raw=reading.raw)

        logger.info(
            "метка собрана из обрезка: урок %d, уцелело %.0f%% площади",
            candidate.lesson.id,
            rebuilt.coverage * 100,
        )
        hit = await self._build_hit(
            candidate, reading.payload, confidence=rebuilt.confidence, box=rebuilt.box
        )
        if hit is None:
            return replace(attempt, raw=reading.raw, damaged=_unknown_student(reading.payload))
        return replace(attempt, hit=hit, raw=reading.raw)

    async def _build_hit(
        self,
        candidate: _Candidate,
        payload: Payload,
        *,
        confidence: float,
        box: tuple[int, int, int, int],
    ) -> TraceHit | None:
        """Собрать вердикт. ``None`` — метка указывает на несуществующего ученика.

        Такого быть не должно: метка пришла из списка выданных, а значит ученик
        есть. Но если запись о нём удалили, назвать всё равно некого, и молча
        отдавать вердикт без имени нельзя.
        """
        async with self._session_factory() as session:
            student = await get_student_by_uid(session, payload.uid)
            if student is None:
                return None
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
        found = await locate_async(suspect, original, hint=candidate.coarse)
        if found is None:
            return None

        size = candidate.image.size
        attempt = _Attempt(
            hit=None,
            confidence=found.confidence,
            found_size=(found.box[2], found.box[3]),
            original_size=size,
            lesson_id=candidate.lesson.id,
        )

        reading = await self._identify(found.crop, size, candidate.lesson)
        if reading.payload is None:
            # Запасной заход: утечка могла быть не скриншотом, а самим файлом,
            # который просто пережали — тогда кроп не нужен вовсе.
            whole = await self._identify(suspect, size, candidate.lesson)
            if whole.payload is not None or reading.raw is None:
                reading = whole

        if reading.payload is None:
            return replace(attempt, damaged=reading.damaged, raw=reading.raw)

        hit = await self._build_hit(
            candidate, reading.payload, confidence=found.confidence, box=found.box
        )
        if hit is None:
            return replace(attempt, raw=reading.raw, damaged=_unknown_student(reading.payload))
        return replace(attempt, hit=hit, raw=reading.raw)

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
                    payload_raw=result.payload_raw,
                    lesson_id=result.best_lesson_id,
                    note=result.reason[:500],
                )
