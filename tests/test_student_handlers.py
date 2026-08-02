"""Поведение student-бота в группе: он там молчит, но фиксирует id чата."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from aiogram import Bot, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import Chat, ChatMemberRestricted, User

from app.bots.student.handlers import common, registration
from app.bots.student.handlers.chats import handle_group_id, on_membership_change
from app.config import Settings
from app.services.membership import check_membership, counts_as_member, is_group_member


class _FakeMember:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeEvent:
    def __init__(self, chat: Chat, status: str) -> None:
        self.chat = chat
        self.new_chat_member = _FakeMember(status)


class _FakeChat:
    def __init__(self, chat_type: str) -> None:
        self.type = chat_type


class _FakeMessage:
    def __init__(self, chat_type: str) -> None:
        self.chat = _FakeChat(chat_type)


@pytest.mark.parametrize("router", [registration.router, common.router])
def test_message_handlers_are_private_only(router: Router) -> None:
    """В группе бот обязан молчать, иначе /start вывалит туда текст оферты."""
    # aiogram оборачивает magic-фильтр и кладёт оригинал в .magic
    magics = [
        item.magic for item in (router.message._handler.filters or []) if item.magic is not None
    ]
    assert magics, f"у роутера {router.name} нет фильтра на тип чата"

    assert all(m.resolve(_FakeMessage("private")) for m in magics)
    assert not any(m.resolve(_FakeMessage("supergroup")) for m in magics)


async def test_group_id_is_logged_when_added(caplog: pytest.LogCaptureFixture) -> None:
    """id группы нигде не показывается в Telegram — берём его из журнала."""
    chat = Chat(id=-1001234567890, type="supergroup", title="VSA PRO")
    with caplog.at_level(logging.INFO):
        await on_membership_change(_FakeEvent(chat, "administrator"))

    assert "VSA_GROUP_ID=-1001234567890" in caplog.text


async def test_warns_when_bot_is_not_admin(caplog: pytest.LogCaptureFixture) -> None:
    """Без прав администратора getChatMember будет отказывать по чужим — предупреждаем."""
    chat = Chat(id=-100999, type="supergroup", title="VSA PRO")
    with caplog.at_level(logging.WARNING):
        await on_membership_change(_FakeEvent(chat, "member"))

    assert "не администратор" in caplog.text


class _FakeGroupMessage:
    def __init__(
        self, chat: Chat, user_id: int | None = None, sender_chat: Chat | None = None
    ) -> None:
        self.chat = chat
        self.from_user = _FakeUser(user_id) if user_id is not None else None
        self.sender_chat = sender_chat
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


def _settings(admin_ids: str) -> Settings:
    return Settings(
        admin_bot_token="a:1",
        student_bot_token="b:2",
        admin_ids=admin_ids,
        wm_pw_img=1,
        wm_pw_wm=2,
    )


async def test_groupid_answers_the_author() -> None:
    chat = Chat(id=-1001234567890, type="supergroup", title="VSA PRO")
    message = _FakeGroupMessage(chat, user_id=987)
    await handle_group_id(message, _settings("987"))
    assert "VSA_GROUP_ID=-1001234567890" in message.replies[0]


async def test_groupid_ignores_everyone_else() -> None:
    """Посторонний в группе не должен даже узнать, что бот отвечает на команды."""
    chat = Chat(id=-100999, type="supergroup", title="VSA PRO")
    message = _FakeGroupMessage(chat, user_id=555)
    await handle_group_id(message, _settings("987"))
    assert message.replies == []


async def test_groupid_answers_anonymous_admin() -> None:
    """В больших группах админы часто пишут анонимно — от имени самой группы.

    Telegram подменяет отправителя служебным ботом, поэтому проверка по
    Telegram ID тут не срабатывает в принципе.
    """
    chat = Chat(id=-1001234567890, type="supergroup", title="VSA PRO")
    message = _FakeGroupMessage(chat, user_id=1087968824, sender_chat=chat)
    await handle_group_id(message, _settings("987"))
    assert "VSA_GROUP_ID=-1001234567890" in message.replies[0]


async def test_groupid_ignores_linked_channel() -> None:
    """Сообщение от связанного канала — не администратор группы."""
    chat = Chat(id=-1001234567890, type="supergroup", title="VSA PRO")
    channel = Chat(id=-1009999999999, type="channel", title="Метод VSA")
    message = _FakeGroupMessage(chat, user_id=1087968824, sender_chat=channel)
    await handle_group_id(message, _settings("987"))
    assert message.replies == []


class _FakeMemberBot:
    """Заглушка getChatMember: отдаёт участника или роняет ошибку Telegram."""

    def __init__(self, *, member: object = None, error: Exception | None = None) -> None:
        self._member = member
        self._error = error

    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        if self._error is not None:
            raise self._error
        return self._member


def _member(status: str, **extra: object) -> object:
    return SimpleNamespace(status=status, **extra)


#: Сбои, при которых членство определить нельзя. Все они — СЁСТРЫ
#: TelegramBadRequest по TelegramAPIError, а не потомки, поэтому узкий
#: except TelegramBadRequest их не ловил и они рвали рассылку целиком.
TRANSIENT = [
    TelegramRetryAfter(method=Mock(), message="Too Many Requests", retry_after=5),
    TelegramForbiddenError(method=Mock(), message="Forbidden: bot was kicked"),
    TelegramNetworkError(method=Mock(), message="Request timeout"),
    TelegramServerError(method=Mock(), message="Bad Gateway"),
    TelegramBadRequest(method=Mock(), message="Bad Request: chat not found"),
]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("creator", True), ("administrator", True), ("member", True),
     ("left", False), ("kicked", False)],
)
async def test_membership_by_status(status: str, expected: bool) -> None:
    bot = cast("Bot", _FakeMemberBot(member=_member(status)))
    assert await is_group_member(bot, -100, 42) is expected


@pytest.mark.parametrize("is_member", [True, False])
async def test_restricted_decided_by_is_member(is_member: bool) -> None:
    """Ограниченному статус остаётся restricted и после выхода из группы.

    Флаг is_member есть только у этого варианта — как раз потому, что статус
    сам по себе тут ничего не говорит. Без него выбывший проходил бы как свой,
    то есть ровно тот случай, ради которого гейт и делался.
    """
    bot = cast("Bot", _FakeMemberBot(member=_member("restricted", is_member=is_member)))
    assert await is_group_member(bot, -100, 42) is is_member


async def test_restricted_on_real_model() -> None:
    """То же на настоящей модели aiogram, а не на самодельной заглушке."""
    left = ChatMemberRestricted.model_construct(
        status="restricted", user=User(id=42, is_bot=False, first_name="Никита"), is_member=False
    )
    assert counts_as_member(left) is False


async def test_absent_user_is_not_a_failure() -> None:
    """Telegram отвечает ошибкой, а не статусом «вышел», если человека в группе нет.

    Это ответ «нет», и человеку надо сказать «вступите в группу», а не гнать его
    к администратору с жалобой на поломку.
    """
    error = TelegramBadRequest(method=Mock(), message="Bad Request: PARTICIPANT_ID_INVALID")
    bot = cast("Bot", _FakeMemberBot(error=error))
    assert await is_group_member(bot, -100, 42) is False


@pytest.mark.parametrize("error", TRANSIENT, ids=lambda e: type(e).__name__)
async def test_transient_failure_is_unknown_not_absent(error: Exception) -> None:
    """Сбой обязан отличаться от «нет в группе», иначе своих начнут выгонять."""
    bot = cast("Bot", _FakeMemberBot(error=error))
    assert await check_membership(bot, -100, 42) is None


@pytest.mark.parametrize("error", TRANSIENT, ids=lambda e: type(e).__name__)
async def test_transient_failure_propagates_from_low_level(error: Exception) -> None:
    """is_group_member пробрасывает сбой: решение о нём принимает вызывающий.

    У регистрации и у рассылки это решение разное, поэтому оно не здесь.
    """
    bot = cast("Bot", _FakeMemberBot(error=error))
    with pytest.raises(TelegramAPIError):
        await is_group_member(bot, -100, 42)


async def test_private_chat_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    chat = Chat(id=42, type="private")
    with caplog.at_level(logging.INFO):
        await on_membership_change(_FakeEvent(chat, "member"))

    assert "VSA_GROUP_ID" not in caplog.text
