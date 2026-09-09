"""Место на томе: сколько занято и что можно убрать.

Появилось после того, как том кончился и бот перестал принимать картинки —
и от автора, и от учеников. Ошибка при этом выглядела как «не удалось
прочитать картинку: [Errno 28] No space left on device», то есть жаловалась
на картинку, хотя дело было в диске.

Причина накопительная. На каждый урок раздача создаёт по персональной копии
на ученика: сто учеников — сто файлов, полторы сотни уроков — пятнадцать
тысяч. Никто их не удалял, потому что :class:`Storage` по устройству ничего
не удаляет сам.

Что здесь считается мусором, а что нет:

* ``marked/`` — **кэш**. Копия собирается детерминированно из оригинала и
  метки, при повторной рассылке появится заново. Удалять можно.
* ``staging/`` — брошенные черновики. Удалять можно, но только старые:
  свежий может прямо сейчас собирать автор.
* ``lessons/`` — **пристинные оригиналы, трогать нельзя никогда**. Без них
  трассировка невозможна в принципе: именно по ним ищется область урока.
* ``traces/`` и ``questions/`` — присланное людьми. Это доказательная база и
  переписка, восстановить их неоткуда, поэтому уборка их не касается.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.storage import Storage

logger = logging.getLogger(__name__)

#: Насколько заполненный том уже считается тревожным. Меньше этого свободного
#: места — и очередная картинка не запишется, а бот скажет про «не удалось
#: прочитать»: до пользователя настоящая причина не доходит.
LOW_SPACE = 0.10

#: Черновики моложе этого срока не трогаем: автор может собирать урок прямо
#: сейчас, и удалённая из-под рук картинка выглядела бы как поломка.
STAGING_KEEP = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Usage:
    """Сколько места на томе и куда оно ушло."""

    total: int
    free: int
    by_folder: dict[str, tuple[int, int]]
    """Папка → (байты, число файлов)."""

    @property
    def used(self) -> int:
        return self.total - self.free

    @property
    def free_share(self) -> float:
        return self.free / self.total if self.total else 0.0

    @property
    def tight(self) -> bool:
        return self.free_share < LOW_SPACE


def human(size: int) -> str:
    """Размер словами. Голые байты в отчёте читать невозможно."""
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(value) < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def folder_size(path: Path) -> tuple[int, int]:
    """Байты и число файлов внутри, рекурсивно."""
    total = 0
    count = 0
    if not path.exists():
        return 0, 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            count += 1
    return total, count


def usage(storage: Storage) -> Usage:
    """Что занимает том прямо сейчас."""
    disk = shutil.disk_usage(storage.root)
    folders = {
        "marked": storage.marked_root,
        "lessons": storage.lessons_root,
        "traces": storage.traces_root,
        "staging": storage.staging_root,
        "questions": storage.questions_root,
    }
    return Usage(
        total=disk.total,
        free=disk.free,
        by_folder={name: folder_size(path) for name, path in folders.items()},
    )


def purge_marked(storage: Storage) -> tuple[int, int]:
    """Удалить персональные копии. Возвращает (освобождено байт, файлов).

    Оригиналы не трогаются — они лежат в другой папке, и это принципиально:
    копию можно собрать заново, оригинал нельзя.
    """
    freed = 0
    removed = 0
    for item in storage.marked_root.rglob("*"):
        if not item.is_file():
            continue
        size = item.stat().st_size
        try:
            item.unlink()
        except OSError as exc:
            logger.warning("не удалось удалить %s: %s", item, exc)
            continue
        freed += size
        removed += 1
    for folder in sorted(storage.marked_root.glob("*"), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return freed, removed


def purge_staging(storage: Storage, older_than: timedelta = STAGING_KEEP) -> tuple[int, int]:
    """Удалить брошенные черновики старше срока."""
    edge = datetime.now(UTC) - older_than
    freed = 0
    removed = 0
    for folder in storage.staging_root.glob("*"):
        if not folder.is_dir():
            continue
        touched = datetime.fromtimestamp(folder.stat().st_mtime, UTC)
        if touched > edge:
            continue
        size, count = folder_size(folder)
        shutil.rmtree(folder, ignore_errors=True)
        freed += size
        removed += count
    return freed, removed


def report(current: Usage) -> list[str]:
    """Отчёт строками — общий для команды в чате и для логов."""
    lines = [
        f"том: {human(current.total)}, свободно {human(current.free)} "
        f"({current.free_share:.0%})"
    ]
    if current.tight:
        lines.append("⚠️ МЕСТО НА ИСХОДЕ: картинки перестанут приниматься")
    for name, (size, count) in sorted(
        current.by_folder.items(), key=lambda item: -item[1][0]
    ):
        lines.append(f"   {name}: {human(size)}, файлов {count}")
    return lines
