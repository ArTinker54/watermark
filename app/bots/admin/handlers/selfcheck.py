"""Самопроверка метки на боевых данных.

Нужна, когда трассировка отвечает «метка не читается», а почему — непонятно.
Присланная картинка не отвечает на главный вопрос: метки в ней нет, потому что
её туда не вшили, или потому что вшивание сломано? Отличить эти два случая по
одной картинке нельзя, а разница принципиальная.

Поэтому проверка идёт не по присланному, а по тому, что лежит на сервере:
берётся копия, которую бот реально отправил ученику, и метка читается прямо из
неё. Плюс свежий круг «вшить и тут же прочитать» — он проверяет движок и пароли
в текущем окружении, а не в тестах на другой машине.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape as quote
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import get_lesson, list_issued_payloads, list_lessons
from app.services import Storage
from app.watermark import Payload, WatermarkEngine, load_bgr, locate
from app.watermark.engine import EMBED_VERSION

logger = logging.getLogger(__name__)

router = Router(name="selfcheck")

#: Сколько выданных копий вскрывать. Больше незачем: если сломано, это видно
#: на первой же, а каждое чтение занимает секунду процессорного времени.
_SAMPLE = 3


def _uid_from_name(path: Path) -> int | None:
    """Номер участника из имени файла ``0042_0_v3.png``."""
    head = path.name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


@router.message(Command("selftest"))
async def selftest(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    storage: Storage,
    engine: WatermarkEngine,
) -> None:
    """Проверить, что метка вшивается и читается — на боевом материале."""
    raw = (command.args or "").strip().lstrip("#")
    if raw.isdigit():
        lesson = await get_lesson(session, int(raw))
    else:
        # Последний урок с картинками: у текстовых и видео метки в пикселях нет.
        lessons = [item for item in await list_lessons(session) if item.images]
        lesson = lessons[0] if lessons else None

    if lesson is None:
        await message.answer(
            "Не нашёл урок с картинками. Укажите номер: <code>/selftest 141</code>"
        )
        return

    status = await message.answer(f"Проверяю урок #{lesson.id}…")
    lines = [f"<b>Самопроверка по уроку #{lesson.id}</b>", quote(lesson.title), ""]

    original = Path(lesson.images[0].path)
    if not original.exists():  # noqa: ASYNC240 - локальная ФС, микросекунды
        await status.edit_text(
            "\n".join(
                [
                    *lines,
                    "❌ <b>Оригинал пропал с диска</b>",
                    f"<code>{quote(str(original))}</code>",
                    "",
                    "Без оригинала трассировка невозможна в принципе: именно по "
                    "нему ищется область урока и восстанавливается геометрия.",
                ]
            )
        )
        return

    width, height = lesson.images[0].size
    lines.append(f"Оригинал: {width}×{height}")

    # --- 1. Движок и пароли прямо сейчас ---------------------------------
    probe = Payload(uid=1, lesson_id=lesson.id)
    scratch = storage.traces_root / f"selftest_{lesson.id}.png"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(engine.embed, original, scratch, probe)
        back = await asyncio.to_thread(engine.extract, scratch, (width, height))
    except Exception as exc:  # noqa: BLE001 - сообщаем автору любую поломку
        logger.exception("самопроверка: встраивание сорвалось")
        lines.append(f"❌ Вшить метку не удалось: {quote(str(exc))}")
        back = None
    finally:
        scratch.unlink(missing_ok=True)

    if back is not None and back.encode() == probe.encode():
        lines.append("✅ Вшить и тут же прочитать — работает")
    else:
        lines += [
            "❌ <b>Метка не читается даже сразу после вшивания</b>",
            f"вшили <code>{probe.encode()}</code>, прочитали "
            f"<code>{quote(back.encode()) if back else 'ничего'}</code>",
            "",
            "Это поломка движка или паролей, а не качества картинки.",
        ]

    # --- 2. Копии, которые правда ушли ученикам --------------------------
    issued = await list_issued_payloads(session, lesson.id)
    folder = storage.marked_root / str(lesson.id)
    copies = sorted(folder.glob(f"*_v{EMBED_VERSION}.png")) if folder.exists() else []

    lines += [
        "",
        f"Записей о выдаче в журнале: <b>{len(issued)}</b>",
        f"Персональных копий на диске: <b>{len(copies)}</b>",
    ]

    if not copies:
        lines += [
            "",
            "⚠️ <b>Копий нет</b> — по этому уроку не выдавали ни одной меченой "
            "картинки. Значит и метки нигде нет: у любого файла с этим графиком "
            "она не прочитается, потому что её туда не вшивали.",
        ]
        await status.edit_text("\n".join(lines))
        return

    lines.append("")
    good = 0
    for copy in copies[:_SAMPLE]:
        uid = _uid_from_name(copy)
        read = await asyncio.to_thread(engine.extract, copy, (width, height))
        expected = Payload(uid=uid, lesson_id=lesson.id).encode() if uid else None
        if read is not None and read.encode() == expected:
            good += 1
            lines.append(f"✅ <code>{quote(copy.name)}</code> → {read.encode()}")
        else:
            got = quote(read.encode()) if read else "не читается"
            lines.append(f"❌ <code>{quote(copy.name)}</code> → {got}")

    # Похожесть меченой копии с оригиналом — калибровка для чтения отказов.
    # Сам оригинал даёт ровно 1.0000, меченая копия — около 0.998: по этому
    # числу в отказе видно, прислали копию или исходник.
    sample = await asyncio.to_thread(load_bgr, copies[0])
    found = await asyncio.to_thread(locate, sample, original)
    if found is not None:
        lines += [
            "",
            f"Похожесть выданной копии с оригиналом: <b>{found.confidence:.4f}</b>",
            "У самого оригинала здесь ровно 1.0000 — по этому числу в отказе "
            "трассировки видно, прислали копию ученика или исходный файл.",
        ]

    if good == len(copies[:_SAMPLE]):
        lines += [
            "",
            "<b>Итог:</b> метка на месте и читается. Если присланная картинка "
            "не читается — она не из персональной раздачи: это либо сам "
            "оригинал, либо копия из канала, куда материал выложили вручную.",
        ]
    await status.edit_text("\n".join(lines))
