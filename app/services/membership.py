"""Проверка членства в группе курса.

Группа — источник правды о том, кто оплатил: состав в ней ведёт отдельный бот
модерации. Поэтому доступ к материалам сверяется именно с ней.

Правила разбора ответов Telegram живут здесь в одном месте: их используют и
регистрация, и рассылка, а расходиться им нельзя.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ChatMemberUnion

logger = logging.getLogger(__name__)

#: Статусы, при которых человек считается участником группы.
MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})

#: Ошибки getChatMember, означающие «этого человека в группе нет», а не сбой.
#: Telegram в этом случае отвечает ошибкой, а не статусом «вышел».
NOT_A_MEMBER_ERRORS = ("PARTICIPANT_ID_INVALID", "USER_NOT_PARTICIPANT", "USER NOT FOUND")


def counts_as_member(member: ChatMemberUnion) -> bool:
    """Считается ли такой участник состоящим в группе.

    Одного статуса мало. У ``restricted`` есть отдельный флаг ``is_member``, и
    только у него: «состоит ли пользователь в чате на данный момент». Ограничили
    и он вышел — статус останется restricted, а в группе его уже нет. Без этой
    проверки ровно тот случай, ради которого делался гейт, проскакивал бы как
    «в группе».
    """
    if member.status not in MEMBER_STATUSES:
        return False
    return bool(getattr(member, "is_member", True))


async def is_group_member(bot: Bot, group_id: int, user_id: int) -> bool:
    """``True`` — в группе, ``False`` — нет.

    Если проверить не удалось, бросает ``TelegramAPIError``: решение, что делать
    со сбоем, принимает вызывающий, и оно разное. При регистрации отказать
    безопасно — человек повторит попытку. При рассылке отказ означал бы, что
    оплативший ученик молча не получил урок, поэтому там наоборот.
    """
    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
    except TelegramBadRequest as exc:
        if any(marker in str(exc).upper() for marker in NOT_A_MEMBER_ERRORS):
            return False
        raise
    return counts_as_member(member)


async def in_any_course(bot: Bot, chat_ids: Sequence[int], user_id: int) -> bool | None:
    """Состоит ли человек хотя бы в одном из курсов.

    ``None`` — ни один курс проверить не удалось. Если хотя бы один ответил
    «нет», а остальные сломались, это всё равно ``None``: утверждать «его нигде
    нет» на основании части ответов неправильно.
    """
    seen_answer = False
    for chat_id in chat_ids:
        try:
            if await is_group_member(bot, chat_id, user_id):
                return True
            seen_answer = True
        except TelegramAPIError as exc:
            logger.error("курс %s не проверен: %s", chat_id, exc)
    return False if seen_answer else None


async def check_membership(bot: Bot, group_id: int, user_id: int) -> bool | None:
    """То же, но со сбоем в виде ``None`` вместо исключения.

    ``None`` обязан отличаться от ``False``: «вас нет в группе» человек поймёт и
    пойдёт вступать, а «не удалось проверить» отправит его к администратору.
    """
    try:
        return await is_group_member(bot, group_id, user_id)
    except TelegramAPIError as exc:
        logger.error("проверка членства в группе %s не удалась: %s", group_id, exc)
        return None
