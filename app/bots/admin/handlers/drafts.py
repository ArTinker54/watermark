"""Общая судьба черновиков: уборка и ответ, когда сценария уже нет.

Сценарии админки (урок, ответ, трассировка) делят одно состояние на человека и
переключают друг друга. Из-за этого возникали три тихих беды: брошенный черновик
оставлял скачанные картинки в staging навсегда; кнопка «Разослать» после
перезапуска процесса не делала вообще ничего, даже не гасила часики; ``/done`` и
``/cancel`` вне сценария молча проглатывались.

Обработчики отсюда подключаются ПОСЛЕДНИМИ — они ловят лишь то, что не разобрал
ни один сценарий.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.services import Storage

logger = logging.getLogger(__name__)

router = Router(name="drafts")

#: Что ответить на кнопку от сценария, которого больше нет.
_LOST = (
    "Черновик потерян — скорее всего, бот перезапустился. "
    "Соберите материал заново: /newlesson"
)


async def drop_draft(state: FSMContext) -> None:
    """Убрать за брошенным сценарием: папку со скачанным и само состояние."""
    data = await state.get_data()
    staging = data.get("staging")
    if staging:
        Storage.drop_staging(Path(staging))
    await state.clear()


@router.callback_query(F.data.startswith("newlesson:") | F.data.startswith("ans:"))
async def stale_button(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка есть, а сценария за ней нет.

    Черновик живёт в памяти процесса, поэтому переживает не всякий перезапуск.
    Раньше нажатие в такой момент не делало ничего: обработчики висели на
    состоянии, а запасного не было, и у автора просто крутились часики.
    """
    await callback.answer()
    logger.info("нажата кнопка потерянного черновика: %s", callback.data)
    await drop_draft(state)
    if isinstance(callback.message, Message):
        await callback.message.answer(_LOST)


@router.message(Command("done"))
async def stray_done(message: Message) -> None:
    await message.answer(
        "Сейчас ничего не собирается. Новый материал — /newlesson, "
        "ответить на вопрос — /questions."
    )


@router.message(Command("cancel"))
async def stray_cancel(message: Message, state: FSMContext) -> None:
    await drop_draft(state)
    await message.answer("Отменять нечего — сейчас ничего не собирается.")
