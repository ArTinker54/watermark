"""Проверка метки на боевых данных: вшивается ли и читается ли обратно.

Вынесено из обработчика, потому что нужно из двух мест. Из админки — командой
``/selftest``, когда автор хочет проверить сам. Со старта процесса — когда
проверить надо удалённо, а доступа в контейнер нет: отчёт уходит в логи.

Отвечает на вопрос, на который присланная картинка ответить не может: метки в
ней нет, потому что её туда не вшивали, или потому что вшивание сломано.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Lesson
from app.db.repo import (
    get_delivery,
    get_lesson,
    get_student_by_uid,
    list_issued_payloads,
    list_lessons,
)
from app.services.storage import Storage
from app.watermark import Payload, WatermarkEngine, load_bgr, locate
from app.watermark.engine import EMBED_VERSION

logger = logging.getLogger(__name__)

#: Сколько выданных копий вскрывать. Больше незачем: если сломано, это видно
#: на первой же, а каждое чтение занимает секунду процессорного времени.
SAMPLE = 3


def uid_from_name(path: Path) -> int | None:
    """Номер участника из имени файла ``0042_0_v3.png``."""
    head = path.name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


async def check(
    session: AsyncSession,
    storage: Storage,
    engine: WatermarkEngine,
    lesson_id: int | None = None,
    uid: int | None = None,
) -> list[str]:
    """Отчёт строками. Формат общий и для сообщения в чат, и для логов."""
    if lesson_id is not None:
        lesson = await get_lesson(session, lesson_id)
    else:
        # Последний урок с картинками: у текстовых и видео метки в пикселях нет.
        lessons = [item for item in await list_lessons(session) if item.images]
        lesson = lessons[0] if lessons else None

    if lesson is None:
        return ["Не нашёл урок с картинками."]

    lines = [f"урок #{lesson.id}: {lesson.title}"]
    original = Path(lesson.images[0].path)
    if not await asyncio.to_thread(original.exists):
        return [*lines, f"ОРИГИНАЛ ПРОПАЛ С ДИСКА: {original}"]

    width, height = lesson.images[0].size
    lines.append(f"оригинал {width}x{height}: {original}")

    # --- 1. Движок и пароли прямо сейчас ---------------------------------
    probe = Payload(uid=1, lesson_id=lesson.id)
    scratch = storage.traces_root / f"selfcheck_{lesson.id}.png"
    await asyncio.to_thread(scratch.parent.mkdir, parents=True, exist_ok=True)
    back: Payload | None = None
    try:
        await asyncio.to_thread(engine.embed, original, scratch, probe)
        back = await asyncio.to_thread(engine.extract, scratch, (width, height))
    except Exception as exc:  # noqa: BLE001 - показываем автору любую поломку
        logger.exception("самопроверка: встраивание сорвалось")
        lines.append(f"ВШИТЬ НЕ УДАЛОСЬ: {exc}")
    finally:
        await asyncio.to_thread(scratch.unlink, True)

    if back is not None and back.encode() == probe.encode():
        lines.append("круг «вшить и прочитать»: ОК")
    else:
        got = back.encode() if back else "ничего"
        lines.append(f"КРУГ СЛОМАН: вшили {probe.encode()}, прочитали {got}")

    # --- 2. Копии, которые правда ушли ученикам --------------------------
    issued = await list_issued_payloads(session, lesson.id)
    folder = storage.marked_root / str(lesson.id)
    copies: list[Path] = []
    if await asyncio.to_thread(folder.exists):
        pattern = f"*_v{EMBED_VERSION}.png"
        copies = sorted(await asyncio.to_thread(lambda: list(folder.glob(pattern))))

    lines.append(f"записей о выдаче: {len(issued)}, копий на диске: {len(copies)}")
    if not copies:
        lines.append(
            "КОПИЙ НЕТ: по уроку не выдавали меченых картинок, метки нет нигде"
        )
        return lines

    for copy in copies[:SAMPLE]:
        # Имя переменной не ``uid``: тем же именем зовётся разбираемый участник,
        # и затирание превратило бы разбор в отчёт по последней вскрытой копии.
        owner = uid_from_name(copy)
        read = await asyncio.to_thread(engine.extract, copy, (width, height))
        expected = Payload(uid=owner, lesson_id=lesson.id).encode() if owner else None
        ok = read is not None and read.encode() == expected
        got = read.encode() if read else "не читается"
        lines.append(f"{'ОК ' if ok else 'СБОЙ'} {copy.name} -> {got}")

    # Похожесть меченой копии с оригиналом — калибровка для чтения отказов:
    # сам оригинал даёт ровно 1.0000, меченая копия около 0.998.
    sample = await asyncio.to_thread(load_bgr, copies[0])
    found = await asyncio.to_thread(locate, sample, original)
    if found is not None:
        lines.append(f"похожесть выданной копии с оригиналом: {found.confidence:.4f}")

    if uid is not None:
        lines += await _about(session, lesson, uid, (width, height), storage, engine)
    return lines


async def _about(
    session: AsyncSession,
    lesson: Lesson,
    uid: int,
    size: tuple[int, int],
    storage: Storage,
    engine: WatermarkEngine,
) -> list[str]:
    """Разбор одного участника: кто это и что ему выдавали.

    Нужен, когда трассировка назвала номер и его надо сверить с журналом
    руками: совпадает ли метка в лежащей на диске копии с тем, что записано
    в выдаче, и кто вообще этот номер.
    """
    payload = Payload(uid=uid, lesson_id=lesson.id)
    lines = [f"--- разбор участника {uid:04d} по уроку #{lesson.id} ---"]

    student = await get_student_by_uid(session, uid)
    if student is None:
        return [*lines, f"участника с номером {uid:04d} в базе нет"]
    lines.append(
        f"это {student.display_name}, tg_id={student.tg_user_id}, "
        f"статус {student.status.value}"
    )

    delivery = await get_delivery(session, lesson_id=lesson.id, student_id=student.id)
    if delivery is None:
        lines.append("записи о выдаче нет: этот урок ему не отправляли")
    else:
        same = "совпадает" if delivery.wm_payload == payload.encode() else "НЕ СОВПАДАЕТ"
        lines.append(
            f"выдача: {delivery.status.value} от {delivery.delivered_at}, "
            f"метка в журнале {delivery.wm_payload} — {same} с ожидаемой"
        )

    copy = storage.marked(lesson.id, uid, 0)
    if not await asyncio.to_thread(copy.exists):
        return [*lines, f"персональной копии на диске нет: {copy.name}"]

    read = await asyncio.to_thread(engine.extract, copy, size)
    got = read.encode() if read else "не читается"
    verdict = "СХОДИТСЯ" if read is not None and read.encode() == payload.encode() else "РАСХОДИТСЯ"
    lines.append(f"метка из копии {copy.name}: {got} — {verdict}")
    return lines
