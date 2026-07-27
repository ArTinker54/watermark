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


def _may_ask_group_id(message: Message, settings: Settings) -> bool:
    """Автор курса или анонимный администратор ЭТОЙ группы.

    Анонимный админ пишет от имени самой группы: Telegram подменяет отправителя
    служебным ботом, а настоящего прячет, — проверка по Telegram ID тут не
    работает в принципе. Зато ``sender_chat``, равный самому чату, бывает только
    у его администратора, и этого достаточно: id группы не секрет, он ничего
    не открывает сам по себе.

    Сообщение от связанного канала сюда не попадает: у него ``sender_chat``
    другой.
    """
    if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
        return True
    user = message.from_user
    return user is not None and user.id in settings.admin_id_set


@router.message(Command("groupid"), F.chat.type.in_(_GROUP_TYPES))
async def handle_group_id(message: Message, settings: Settings) -> None:
    """Показать id чата. Только администратору — остальным бот в группе молчит.

    Событие о добавлении в группу приходит один раз и только если бот уже
    работал в этот момент; команда же доступна всегда.
    """
    if not _may_ask_group_id(message, settings):
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
