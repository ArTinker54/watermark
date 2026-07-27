"""Поведение student-бота в группе: он там молчит, но фиксирует id чата."""

from __future__ import annotations

import logging

import pytest
from aiogram import Router
from aiogram.types import Chat

from app.bots.student.handlers import common, registration
from app.bots.student.handlers.chats import handle_group_id, on_membership_change
from app.config import Settings


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
    def __init__(self, chat: Chat, user_id: int) -> None:
        self.chat = chat
        self.from_user = _FakeUser(user_id)
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


async def test_private_chat_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    chat = Chat(id=42, type="private")
    with caplog.at_level(logging.INFO):
        await on_membership_change(_FakeEvent(chat, "member"))

    assert "VSA_GROUP_ID" not in caplog.text
