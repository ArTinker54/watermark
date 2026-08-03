"""Черновики админки: уборка, устаревшие кнопки, команды вне сценария."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

from app.bots.admin.handlers.drafts import drop_draft
from app.bots.admin.handlers.drafts import router as drafts_router
from app.bots.admin.handlers.lessons import SEND_CALLBACK, _confirm_keyboard
from app.bots.admin.handlers.questions import SEND_ANON, SEND_NAMED, _send_keyboard
from app.bots.admin.setup import build_admin
from app.bots.student.setup import build_student
from app.config import Settings
from app.db import create_database


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1)
    )


async def test_drop_draft_takes_the_staging_directory_with_it(
    state: FSMContext, tmp_path: Path
) -> None:
    """Брошенный черновик не должен оставлять скачанное на диске навсегда."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "orig_0.png").write_bytes(b"\x89PNG")
    await state.set_data({"staging": str(staging), "images": ["x"]})

    await drop_draft(state)

    assert not staging.exists(), "папка черновика осталась на диске"
    assert await state.get_data() == {}
    assert await state.get_state() is None


async def test_drop_draft_survives_a_missing_directory(state: FSMContext) -> None:
    """Папку могли уже убрать — это не повод падать."""
    await state.set_data({"staging": "/нет/такого/пути"})
    await drop_draft(state)
    assert await state.get_data() == {}


def _course(course_id: int, title: str) -> Mock:
    return Mock(id=course_id, title=title)


def test_lesson_button_carries_the_draft_id() -> None:
    """Без идентификатора черновика кнопка со старого сообщения отправляла
    свежий, ни к чему не относящийся черновик — и в чужую аудиторию."""
    keyboard = _confirm_keyboard([_course(3, "Курс")], "abc123")
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert f"{SEND_CALLBACK}:abc123:3" in data


def test_lesson_button_without_courses_still_carries_the_draft() -> None:
    keyboard = _confirm_keyboard([], "abc123")
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert f"{SEND_CALLBACK}:abc123:0" in data


@pytest.mark.parametrize(
    ("audience", "courses", "expected"),
    [
        ("personal", [], f"{SEND_ANON}:zz99:0"),
        ("all", [], f"{SEND_NAMED}:zz99:0"),
        ("all", [_course(5, "Курс")], f"{SEND_NAMED}:zz99:5"),
    ],
)
def test_answer_buttons_carry_the_draft_id(
    audience: str, courses: list[Mock], expected: str
) -> None:
    keyboard = _send_keyboard(audience, courses, "zz99")
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert expected in data


def test_stray_commands_have_no_state_filter() -> None:
    """Обработчики /done и /cancel вне сценария должны срабатывать всегда.

    Если повесить их на состояние, вернётся прежнее поведение: команда в пустоту
    молча проглатывается, и человек не понимает, услышали его или нет.
    """
    handlers = drafts_router.message.handlers
    assert handlers, "запасные обработчики команд не зарегистрированы"
    for handler in handlers:
        assert not any(
            type(item.callback).__name__ == "StateFilter" for item in handler.filters or []
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_bot_token="a:1",
        student_bot_token="b:2",
        admin_ids="1",
        wm_pw_img=1,
        wm_pw_wm=2,
        db_path=tmp_path / "db.sqlite3",
        storage_path=tmp_path / "storage",
    )


async def test_bots_are_assembled_safely(tmp_path: Path) -> None:
    """Две вещи, которые легко потерять при правке сборки.

    Первая: апдейты одного человека обязаны обрабатываться по очереди. Без
    этого альбом ломался — Telegram шлёт каждое фото отдельным апдейтом,
    обработчики читали состояние одновременно, вычисляли один и тот же номер
    картинки и писали все фото в один файл; двойное нажатие «Разослать» тем же
    способом успевало пройти дважды.

    Вторая: запасные обработчики черновиков стоят ПОСЛЕ сценариев, иначе они
    перехватывали бы /done и /cancel у живого сценария.

    Роутеры — синглтоны на модуль, собрать их дважды нельзя, поэтому обе
    проверки живут в одном тесте.
    """
    settings = _settings(tmp_path)
    database = create_database(settings)
    try:
        student = build_student(settings, database, bot=Mock(), admin_notifier=Mock())
        admin = build_admin(settings, database, student_bot=Mock(), bot=Mock())

        for runtime in (admin, student):
            isolation = runtime.dispatcher.fsm.events_isolation
            assert isinstance(isolation, SimpleEventIsolation), runtime.name

        root = next(
            child for child in admin.dispatcher.sub_routers if child.name == "admin"
        )
        names = [child.name for child in root.sub_routers]
        assert names[-1] == drafts_router.name, names
        assert "lessons" in names and "questions-admin" in names
    finally:
        await database.dispose()
