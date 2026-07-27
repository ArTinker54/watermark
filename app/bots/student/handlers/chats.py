"""Реакция на добавление бота в группу.

Нужно ради одной вещи: узнать id группы для ``VSA_GROUP_ID``. Он нигде не
показывается в интерфейсе Telegram, а без него проверку членства не включить.
Заодно в журнале видно, куда бота добавляли и какие права дали.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from app.config import Settings

logger = logging.getLogger(__name__)

router = Router(name="chats")

_GROUP_TYPES = frozenset({"group", "supergroup"})

#: Статусы, при которых getChatMember стабильно отвечает по чужим участникам.
_CAN_CHECK_MEMBERS = frozenset({"administrator", "creator"})


@router.message(Command("groupid"), F.chat.type.in_(_GROUP_TYPES))
async def handle_group_id(message: Message, settings: Settings) -> None:
    """Показать id чата. Только автору курса — остальным бот в группе молчит.

    Событие о добавлении в группу приходит один раз и только если бот уже
    работал в этот момент; команда же доступна всегда.
    """
    user = message.from_user
    if user is None or user.id not in settings.admin_id_set:
        return

    await message.reply(
        f"<code>VSA_GROUP_ID={message.chat.id}</code>\n\n"
        "Это можно удалить — значение уже записано в журнал бота."
    )
    logger.info("запрошен id чата «%s»: VSA_GROUP_ID=%s", message.chat.title, message.chat.id)


@router.my_chat_member()
async def on_membership_change(event: ChatMemberUpdated) -> None:
    chat = event.chat
    if chat.type == "private":
        return

    status = event.new_chat_member.status
    if status in {"left", "kicked"}:
        logger.info("бота убрали из чата «%s» (id=%s)", chat.title, chat.id)
        return

    logger.info(
        "бот в чате «%s»: VSA_GROUP_ID=%s, права=%s",
        chat.title,
        chat.id,
        status,
    )
    if status not in _CAN_CHECK_MEMBERS:
        # Telegram гарантирует ответ getChatMember по чужим участникам только
        # администратору. Без прав проверка членства будет отказывать всем.
        logger.warning(
            "бот в чате «%s» не администратор — проверка членства работать не будет",
            chat.title,
        )
