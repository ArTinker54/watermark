"""Единая настройка логирования для обоих ботов."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", *, log_file: Path | None = None) -> None:
    """Логи в stdout (их собирает docker) и, по желанию, в файл.

    Доставки и попытки трассировки пишутся на уровне INFO — это рабочий журнал,
    по которому потом восстанавливают, кому что уходило.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level.upper(),
        format=_FORMAT,
        handlers=handlers,
        force=True,
    )
    # aiogram на DEBUG сыплет каждым апдейтом — это шум, не сигнал.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
