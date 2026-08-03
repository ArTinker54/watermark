"""Обновление схемы на уже существующей базе.

create_all добавляет только недостающие таблицы и не трогает существующие.
Значит колонка, появившаяся после первого выпуска, сама собой в боевой базе
не возникнет — и обновлённый код упадёт на ней с «no such column».
Здесь проверяется именно этот путь, а не создание базы с нуля.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db import create_database
from app.db.repo import add_course, list_courses


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_bot_token="a:1",
        student_bot_token="b:2",
        wm_pw_img=1,
        wm_pw_wm=2,
        db_path=tmp_path / "old.sqlite3",
        storage_path=tmp_path / "storage",
    )


#: Схема lessons такой, какой она была до появления вопросов.
_OLD_LESSONS = """
CREATE TABLE lessons (
    id INTEGER NOT NULL PRIMARY KEY,
    admin_tg_id BIGINT NOT NULL,
    caption TEXT,
    original_image_path VARCHAR(512) NOT NULL,
    created_at DATETIME NOT NULL
)
"""


async def test_missing_column_is_added_to_existing_db(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = create_database(settings)

    # Готовим «старую» базу: таблица уроков есть, колонки question_id в ней нет.
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(_OLD_LESSONS)
        await connection.exec_driver_sql(
            "INSERT INTO lessons (id, admin_tg_id, caption, original_image_path, created_at)"
            " VALUES (1, 42, 'старый урок', '/data/orig.png', '2026-01-01 00:00:00')"
        )

    await database.create_all()

    async with database.session_factory() as session:
        info = await session.execute(text("PRAGMA table_info(lessons)"))
        columns = {row[1] for row in info.fetchall()}
        assert "question_id" in columns, "колонка не добавлена — боевая база сломается"

        # Старая запись цела, новая колонка пустая.
        row = await session.execute(text("SELECT caption, question_id FROM lessons WHERE id = 1"))
        assert row.fetchone() == ("старый урок", None)

        # И новая таблица тоже появилась.
        tables = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        assert "questions" in {row[0] for row in tables.fetchall()}

    await database.dispose()


async def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    """Повторный запуск не должен падать: боты стартуют по многу раз."""
    settings = _settings(tmp_path)
    database = create_database(settings)
    await database.create_all()
    await database.create_all()
    await database.create_all()

    async with database.session_factory() as session:
        indexes = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='lessons'")
        )
        names = [row[0] for row in indexes.fetchall()]
        assert names.count("ix_lessons_question_id") == 1

    await database.dispose()


async def test_two_processes_can_start_at_once(tmp_path: Path) -> None:
    """Одновременный подъём схемы не должен ронять ни одного из ботов.

    create_all идемпотентен, но не атомарен: проверка существования и CREATE —
    разные шаги. При перезапуске внахлёст второй процесс падал на «already
    exists», хотя схему уже создал сосед и работать было можно.
    """
    settings = _settings(tmp_path)

    async def boot() -> None:
        database = create_database(settings)
        try:
            await database.create_all()
            async with database.session_factory() as session:
                await add_course(session, title="Курс", chat_id=-100500)
        finally:
            await database.dispose()

    await asyncio.gather(*(boot() for _ in range(4)))

    database = create_database(settings)
    try:
        async with database.session_factory() as session:
            courses = list(await list_courses(session, only_active=False))
    finally:
        await database.dispose()

    assert len(courses) == 1, "курс с тем же чатом обязан остаться один"


async def test_broken_schema_still_fails_loudly(tmp_path: Path) -> None:
    """Проглатывать надо только гонку: испорченная база обязана падать громко."""
    settings = _settings(tmp_path)
    database = create_database(settings)
    try:
        with pytest.raises(OperationalError):
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql("SELECT * FROM таблицы_нет")
    finally:
        await database.dispose()
