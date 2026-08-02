"""Обновление схемы на уже существующей базе.

create_all добавляет только недостающие таблицы и не трогает существующие.
Значит колонка, появившаяся после первого выпуска, сама собой в боевой базе
не возникнет — и обновлённый код упадёт на ней с «no such column».
Здесь проверяется именно этот путь, а не создание базы с нуля.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.config import Settings
from app.db import create_database


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
