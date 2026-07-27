"""Раскладка файлов на диске.

::

    storage/
      staging/<token>/orig_0.png      — черновик урока, пока автор его собирает
      lessons/<lesson_id>/orig_0.png  — ПРИСТИННЫЙ оригинал, без метки
      marked/<lesson_id>/<uid>_0.png  — персональная меченая копия
      traces/<stamp>_<token>.png      — что прислали на трассировку
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Storage:
    """Пути внутри общего тома. Ничего не удаляет само."""

    root: Path

    def __post_init__(self) -> None:
        for directory in (self.staging_root, self.lessons_root, self.marked_root, self.traces_root):
            directory.mkdir(parents=True, exist_ok=True)

    # --- Корни ---

    @property
    def staging_root(self) -> Path:
        return self.root / "staging"

    @property
    def lessons_root(self) -> Path:
        return self.root / "lessons"

    @property
    def marked_root(self) -> Path:
        return self.root / "marked"

    @property
    def traces_root(self) -> Path:
        return self.root / "traces"

    # --- Черновик урока ---

    def new_staging_dir(self) -> Path:
        directory = self.staging_root / uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def staged_original(staging: Path, position: int) -> Path:
        return staging / f"orig_{position}.png"

    @staticmethod
    def drop_staging(staging: Path) -> None:
        shutil.rmtree(staging, ignore_errors=True)

    # --- Урок ---

    def lesson_dir(self, lesson_id: int) -> Path:
        directory = self.lessons_root / str(lesson_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def original(self, lesson_id: int, position: int) -> Path:
        return self.lesson_dir(lesson_id) / f"orig_{position}.png"

    def marked(self, lesson_id: int, uid: int, position: int) -> Path:
        directory = self.marked_root / str(lesson_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{uid:04d}_{position}.png"

    # --- Трассировка ---

    def trace_input(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.traces_root / f"{stamp}_{uuid4().hex[:8]}.png"
