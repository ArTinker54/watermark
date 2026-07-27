"""Трассировка утечки: скрин -> uid -> личность."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.bots.files import ImageRejected, download_image, extract_file_id
from app.services import Storage, TraceHit, TraceMiss, TraceResult, TraceService
from app.utils import format_dt

logger = logging.getLogger(__name__)

router = Router(name="trace")


class Trace(StatesGroup):
    waiting = State()


@router.message(Command("trace"))
async def start_trace(message: Message, state: FSMContext) -> None:
    await state.set_state(Trace.waiting)
    await message.answer(
        "Пришлите утёкшую картинку или скриншот экрана.\n\n"
        "Скриншот целиком — это нормально: область урока найдётся сама. "
        "Если есть исходный файл, лучше слать <b>файлом</b>: без пережатия "
        "метка читается увереннее."
    )


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
        lines = ["<b>Метку прочитать не удалось</b>", "", result.reason]
        if result.best_confidence is not None:
            lines.append("")
            lines.append(
                f"Лучшее совпадение: {result.best_confidence:.0%} "
                f"(урок #{result.best_lesson_id})"
            )
        lines.append(f"Проверено оригиналов: {result.checked}")
        return "\n".join(lines)

    return _format_hit(result)


def _format_hit(hit: TraceHit) -> str:
    x, y, width, height = hit.box
    lines = [
        "<b>Метка найдена</b>",
        "",
        f"Урок: <b>#{hit.lesson.id}</b> от {format_dt(hit.lesson.created_at)}",
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

    lines += [
        "",
        f"Метка: <code>{hit.payload.encode()}</code>",
        f"Совпадение с оригиналом: {hit.confidence:.0%}",
        f"Область на картинке: {width}×{height} в точке ({x}, {y})",
    ]

    if hit.delivery_confirmed:
        lines.append("Доставка подтверждена журналом.")
    else:
        lines.append(
            "Записи о такой доставке в журнале нет — проверьте, "
            "не пересылался ли материал вручную."
        )
    return "\n".join(lines)
