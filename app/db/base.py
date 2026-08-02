"""Подключение к SQLite: движок, фабрика сессий, PRAGMA."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.db.models import Base

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Database:
    """Движок и фабрика сессий. Живёт один экземпляр на процесс."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def create_all(self) -> None:
        """Привести схему в актуальное состояние.

        ``create_all`` идемпотентен, но добавляет только недостающие ТАБЛИЦЫ:
        существующие он не трогает вовсе. Поэтому колонки, появившиеся после
        первого выпуска, дописываются отдельно — иначе обновлённый код упал бы
        на боевой базе с «no such column».
        """
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await _add_missing_columns(connection)
        logger.info("схема БД готова")

    async def dispose(self) -> None:
        await self.engine.dispose()


#: Колонки, добавленные после первого выпуска: (таблица, колонка, тип, индекс).
#: Полноценный alembic для такой схемы избыточен, но и молча ломать боевую
#: базу нельзя — список ведётся руками и применяется на старте.
_ADDED_COLUMNS: tuple[tuple[str, str, str, str | None], ...] = (
    ("lessons", "question_id", "INTEGER REFERENCES questions(id)", "ix_lessons_question_id"),
)


async def _add_missing_columns(connection: AsyncConnection) -> None:
    for table, column, ddl, index in _ADDED_COLUMNS:
        info = await connection.exec_driver_sql(f"PRAGMA table_info({table})")
        columns = {row[1] for row in info.fetchall()}
        if not columns:
            continue  # таблицы ещё нет — create_all создаст её сразу с колонкой
        if column not in columns:
            await connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            logger.info("схема: добавлена колонка %s.%s", table, column)
        if index:
            await connection.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})"
            )


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """WAL — чтобы два бота писали в одну базу без «database is locked»."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()


def create_database(settings: Settings, *, echo: bool = False) -> Database:
    db_path: Path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(settings.database_url, echo=echo, future=True)
    event.listen(engine.sync_engine, "connect", _apply_pragmas)

    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return Database(engine=engine, session_factory=session_factory)
