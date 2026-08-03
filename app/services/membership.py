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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberUnion

logger = logging.getLogger(__name__)

#: Статусы, при которых человек считается участником группы.
MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})

#: Ошибки getChatMember, означающие «этого человека в группе нет», а не сбой.
#: Telegram в этом случае отвечает ошибкой, а не статусом «вышел».
NOT_A_MEMBER_ERRORS = ("PARTICIPANT_ID_INVALID", "USER_NOT_PARTICIPANT", "USER NOT FOUND")

#: Ошибки, означающие, что недоступен сам чат курса: неверный id, бота туда не
#: добавили или выкинули. Это не сбой связи — повторять бесполезно. Отличать их
#: обязательно: при рассылке сбой трактуется как «выдать материал», и на такой
#: ошибке материал ушёл бы вообще всем, а отчёт остался бы чистым.
CHAT_UNREACHABLE_ERRORS = (
    "CHAT_NOT_FOUND",
    "CHANNEL_INVALID",
    "CHANNEL_PRIVATE",
    "PEER_ID_INVALID",
    "BOT_IS_NOT_A_MEMBER",
    "CHAT_ADMIN_REQUIRED",
    "CHAT_WRITE_FORBIDDEN",
    "MEMBER_LIST_IS_INACCESSIBLE",
    "NOT_ENOUGH_RIGHTS",
    "GROUP_CHAT_WAS_UPGRADED",
)


def _normalised(text: str) -> str:
    """Одну и ту же беду Telegram пишет по-разному.

    В одних ответах это ``CHAT_NOT_FOUND``, в других — «Bad Request: chat not
    found». Сравнивать надо в общем виде, иначе половина случаев проходит мимо:
    ровно на этом ошибка «чат не найден» считалась случайным сбоем, и материал
    уходил всем подряд.
    """
    return text.upper().replace("_", " ")


class CourseUnreachable(RuntimeError):
    """Чат курса недоступен по постоянной причине — проверять допуск нечем."""

    def __init__(self, chat_id: int, reason: str) -> None:
        super().__init__(f"чат курса {chat_id} недоступен: {reason}")
        self.chat_id = chat_id
        self.reason = reason


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

    Отдельно выделен случай, когда недоступен сам чат: тогда летит
    ``CourseUnreachable``. Трактовать его как обычный сбой нельзя — «не смогли
    проверить, значит выдаём» превратилось бы в раздачу материала всем подряд.
    """
    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
    except TelegramBadRequest as exc:
        text = _normalised(str(exc))
        if any(_normalised(marker) in text for marker in NOT_A_MEMBER_ERRORS):
            return False
        if any(_normalised(marker) in text for marker in CHAT_UNREACHABLE_ERRORS):
            raise CourseUnreachable(group_id, str(exc)) from exc
        raise
    except TelegramForbiddenError as exc:
        # Бота выкинули из чата курса или разжаловали: гейт мёртв, а не «сбоит».
        raise CourseUnreachable(group_id, str(exc)) from exc
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
        except (TelegramAPIError, CourseUnreachable) as exc:
            # Недоступный курс здесь не фатален: у человека может быть право по
            # другому, и один сломанный чат не повод отказывать всем.
            logger.error("курс %s не проверен: %s", chat_id, exc)
    return False if seen_answer else None


async def check_membership(bot: Bot, group_id: int, user_id: int) -> bool | None:
    """То же, но со сбоем в виде ``None`` вместо исключения.

    ``None`` обязан отличаться от ``False``: «вас нет в группе» человек поймёт и
    пойдёт вступать, а «не удалось проверить» отправит его к администратору.
    """
    try:
        return await is_group_member(bot, group_id, user_id)
    except (TelegramAPIError, CourseUnreachable) as exc:
        logger.error("проверка членства в группе %s не удалась: %s", group_id, exc)
        return None
