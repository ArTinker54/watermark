"""Когда бот в личке говорит, а когда молчит.

Правило здесь одно и оно важнее всей остальной механики: **живой человек
всегда главнее бота**. Как только Алёна ответила в переписке сама, бот в этом
чате замолкает — иначе он влезает репликой в живой разговор, и это выглядит
хуже, чем если бы его вообще не было.

Решение вынесено из обработчиков в чистую функцию намеренно: у неё нет ни
Telegram, ни сети, поэтому её поведение можно проверить тестами, а не
переписками на боевом аккаунте.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import DmDialog, utcnow

#: Насколько бот замолкает после реплики автора. Не «навсегда»: через месяц
#: тишины это уже новый разговор, и меню снова уместно. Не «на час»: пока
#: человека ведут вручную, вмешательство бота только мешает.
MUTE = timedelta(days=30)

#: Как часто можно повторять меню тому, кто пишет свободным текстом. Без
#: паузы человек, отправивший пять сообщений подряд, получил бы пять меню.
NUDGE_COOLDOWN = timedelta(hours=6)

#: Как часто один человек может позвать автора. Защита не от людей, а от
#: случайного двойного нажатия и от нетерпеливых.
CALL_COOLDOWN = timedelta(hours=1)

#: Насколько бот умолкает после того, как человека передали автору.
#:
#: Кнопка «Позвать Алёну» отвечает приглашением задать вопрос — значит на этот
#: вопрос бот отвечать не должен: «такого нет в списке» в ответ на прямую
#: просьбу к человеку выглядит издевательством.
#:
#: Три дня, а не месяц: автор обещает ответить в течение дня, и трёх суток
#: хватает с запасом на выходные. Если она ответит — молчание и так продлится
#: на месяц её же репликой. Если не ответит вовсе, разговор не окажется
#: заперт навсегда и меню вернётся.
HANDOVER = timedelta(days=3)


class Reply(enum.StrEnum):
    """Что бот делает с входящим сообщением."""

    SILENT = "silent"
    """Ничего. Разговор ведёт человек."""

    GREET = "greet"
    """Поздороваться и показать меню — человек пишет впервые."""

    NUDGE = "nudge"
    """Меню без приветствия — вопрос свободным текстом, отвечать угадайкой нельзя."""


def decide(dialog: DmDialog | None, now: datetime | None = None) -> Reply:
    """Что ответить на входящее сообщение в этом чате."""
    now = now or utcnow()
    if dialog is None:
        return Reply.GREET
    if dialog.muted_until is not None:
        if now < dialog.muted_until:
            return Reply.SILENT
        # Молчание кончилось. Продолжать с середины нельзя: спустя месяц это
        # уже другой разговор, и «такого вопроса в списке нет» прозвучало бы
        # ответом на реплику, которой не было. Начинаем заново.
        return Reply.GREET
    if dialog.greeted_at is None:
        return Reply.GREET
    if dialog.nudged_at is None or now - dialog.nudged_at >= NUDGE_COOLDOWN:
        return Reply.NUDGE
    return Reply.SILENT


def may_call(dialog: DmDialog | None, now: datetime | None = None) -> bool:
    """Можно ли прямо сейчас дёрнуть автора по этому чату."""
    now = now or utcnow()
    if dialog is None or dialog.called_at is None:
        return True
    return now - dialog.called_at >= CALL_COOLDOWN


def viewed_keys(dialog: DmDialog | None) -> list[str]:
    """Какие ответы человек уже открывал, в порядке открытия."""
    if dialog is None or not dialog.viewed:
        return []
    return [key for key in dialog.viewed.split(",") if key]


async def get(session: AsyncSession, chat_id: int) -> DmDialog | None:
    result = await session.execute(
        select(DmDialog).where(DmDialog.chat_id == chat_id)
    )
    return result.scalar_one_or_none()


async def touch(session: AsyncSession, chat_id: int, connection_id: str) -> DmDialog:
    """Найти разговор или завести новый, попутно освежив адрес для ответа.

    ``connection_id`` перевыпускается, когда автор переподключает бота в
    настройках. Старый после этого не работает, поэтому запоминаем свежий на
    каждом сообщении, а не только при создании записи.
    """
    dialog = await get(session, chat_id)
    if dialog is None:
        dialog = DmDialog(chat_id=chat_id, connection_id=connection_id)
        session.add(dialog)
        await session.commit()
    elif dialog.connection_id != connection_id:
        dialog.connection_id = connection_id
        await session.commit()
    return dialog


async def mark_greeted(session: AsyncSession, dialog: DmDialog) -> None:
    """Поздоровались — и заодно сняли отметку молчания, если она догорела.

    Без этого разговор, отмолчавший своё, здоровался бы на каждое сообщение:
    ``decide`` смотрит на ``muted_until``, а не на дату приветствия.
    """
    dialog.greeted_at = utcnow()
    dialog.muted_until = None
    dialog.nudged_at = None
    await session.commit()


async def mark_nudged(session: AsyncSession, dialog: DmDialog) -> None:
    dialog.nudged_at = utcnow()
    await session.commit()


async def mark_called(session: AsyncSession, dialog: DmDialog) -> None:
    """Человека передали автору — и бот в этом разговоре умолкает.

    Замолчать здесь так же обязательно, как после реплики самой Алёны: ответ
    приглашает задать вопрос, и встревать в него подсказками нельзя.
    """
    now = utcnow()
    dialog.called_at = now
    dialog.muted_until = now + HANDOVER
    await session.commit()


async def mark_viewed(session: AsyncSession, dialog: DmDialog, key: str) -> None:
    """Запомнить открытый ответ — он попадёт в карточку, которую увидит автор."""
    keys = viewed_keys(dialog)
    if key in keys:
        return
    keys.append(key)
    # Колонка не резиновая, а список нужен только чтобы понять контекст
    # вопроса: последние открытые ответы информативнее самых первых.
    dialog.viewed = ",".join(keys[-20:])
    await session.commit()


async def mute(session: AsyncSession, chat_id: int, connection_id: str) -> None:
    """Автор ответила сама — бот в этом чате умолкает."""
    dialog = await touch(session, chat_id, connection_id)
    dialog.muted_until = utcnow() + MUTE
    await session.commit()
