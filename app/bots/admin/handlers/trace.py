"""Трассировка утечки: скрин -> uid -> личность."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.bots.files import ImageRejected, download_image, extract_file_id
from app.db import DeliveryStatus
from app.services import Storage, TraceHit, TraceMiss, TraceResult, TraceService
from app.utils import format_dt
from app.watermark import READABLE_SCALE
from app.watermark.text import has_marks

logger = logging.getLogger(__name__)

router = Router(name="trace")


class Trace(StatesGroup):
    waiting = State()


@router.message(Command("trace"))
async def start_trace(message: Message, state: FSMContext) -> None:
    await state.set_state(Trace.waiting)
    await message.answer(
        "Пришлите утёкшую картинку, скриншот экрана <b>или скопированный текст</b>.\n\n"
        "Скриншот целиком — это нормально: область урока найдётся сама. "
        "Если есть исходный файл, лучше слать <b>файлом</b>: без пережатия "
        "метка читается увереннее.\n\n"
        "Текст должен быть именно скопирован, а не перепечатан: метка в нём "
        "живёт в невидимых символах."
    )


@router.message(StateFilter(Trace.waiting, None), F.text, ~F.text.startswith("/"))
async def run_text_trace(
    message: Message, state: FSMContext, tracer: TraceService
) -> None:
    """Опознать по присланному тексту.

    Реагируем только если в тексте вправду есть невидимые символы, иначе бот
    отвечал бы на каждую заметку автора самому себе.
    """
    text = message.text or ""
    if not has_marks(text) and await state.get_state() is None:
        return

    if message.from_user is None:
        return
    await state.clear()
    status = await message.answer("Ищу метку в тексте…")
    result = await tracer.trace_text(text, admin_tg_id=message.from_user.id)
    await status.edit_text(_format(result))


@router.message(StateFilter(Trace.waiting, None), F.photo | F.document)
async def run_trace(
    message: Message,
    state: FSMContext,
    bot: Bot,
    storage: Storage,
    tracer: TraceService,
) -> None:
    if message.from_user is None:
        return

    target = storage.trace_input()
    try:
        file_id = extract_file_id(message)
        await download_image(bot, file_id, target)
    except ImageRejected as exc:
        await message.answer(f"Не принято: {exc}")
        return

    await state.clear()
    status = await message.answer("Ищу метку…")
    result = await tracer.trace(target, admin_tg_id=message.from_user.id)
    await status.edit_text(_format(result))


def _format(result: TraceResult) -> str:
    if isinstance(result, TraceMiss):
        return _format_miss(result)
    return _format_hit(result)


def _format_miss(miss: TraceMiss) -> str:
    lines = ["<b>Метку прочитать не удалось</b>", "", miss.reason, ""]

    if miss.scale is not None and miss.scale < READABLE_SCALE:
        # Тут важно не просто отказать, а сказать, что сделать по-другому.
        lines += [
            "<b>Что поможет:</b>",
            "• переслать сам файл или картинку из чата, а не снимок всего экрана;",
            "• если это снимок экрана — открыть картинку на весь экран и снять её так;",
            "• не уменьшать и не обрезать картинку перед отправкой.",
            "",
        ]

    if miss.best_lesson_id is not None:
        lines.append(f"Похоже на урок #{miss.best_lesson_id}")
    lines.append(f"Проверено оригиналов: {miss.checked}")
    return "\n".join(lines)


def _format_hit(hit: TraceHit) -> str:
    lines = [
        "<b>Метка найдена</b>",
        "",
        f"Материал: <b>{hit.lesson.title}</b> от {format_dt(hit.lesson.created_at)}",
    ]

    if hit.student is not None:
        lines += [
            f"Участник: <b>{hit.payload.uid:04d}</b> — {hit.student.display_name}",
            f"Telegram ID: <code>{hit.student.tg_user_id}</code>",
        ]
    else:
        lines.append(
            f"Участник: <b>{hit.payload.uid:04d}</b> — в базе такого нет "
            "(запись удалена?)"
        )

    lines += ["", f"Метка: <code>{hit.payload.encode()}</code>", f"Источник: {hit.source}"]
    if hit.confidence is not None and hit.box is not None:
        x, y, width, height = hit.box
        lines += [
            f"Совпадение с оригиналом: {hit.confidence:.0%}",
            f"Область на картинке: {width}×{height} в точке ({x}, {y})",
        ]

    lines.append(_delivery_note(hit))
    return "\n".join(lines)


def _delivery_note(hit: TraceHit) -> str:
    """Что говорит журнал о выдаче именно этой копии.

    Три разных случая, и путать их нельзя: запись об успешной выдаче, запись об
    ошибке (копию сделали и записали, но отправка сорвалась — метка всё равно
    его) и отсутствие записи вовсе.
    """
    delivery = hit.delivery
    if delivery is None or delivery.wm_payload != hit.payload.encode():
        return (
            "Записи о такой выдаче в журнале нет — проверьте, "
            "не пересылался ли материал вручную."
        )
    if delivery.status is DeliveryStatus.SENT:
        return "Выдача подтверждена журналом."
    if delivery.status is DeliveryStatus.FAILED:
        return (
            "В журнале запись об ошибке отправки: копия с этой меткой была "
            f"сделана именно для него ({format_dt(delivery.delivered_at)}), "
            "но доставка сорвалась. Метка всё равно его."
        )
    return (
        "В журнале запись о пропуске: копию для него не создавали. "
        "Похоже, материал попал к нему не через бота."
    )
