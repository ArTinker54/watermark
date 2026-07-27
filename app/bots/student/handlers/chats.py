"""Реакция на добавление бота в группу.

Нужно ради одной вещи: узнать id группы для ``VSA_GROUP_ID``. Он нигде не
показывается в интерфейсе Telegram, а без него проверку членства не включить.
Заодно в журнале видно, куда бота добавляли и какие права дали.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ChatMemberUpdated

logger = logging.getLogger(__name__)

router = Router(name="chats")

#: Статусы, при которых getChatMember стабильно отвечает по чужим участникам.
_CAN_CHECK_MEMBERS = frozenset({"administrator", "creator"})


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
