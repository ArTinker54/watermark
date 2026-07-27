"""Сборка урока из черновика: перенос пристинных оригиналов в постоянное место."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Lesson
from app.db.repo import ImageSpec, create_lesson
from app.services.storage import Storage
from app.watermark import image_size

logger = logging.getLogger(__name__)


async def save_lesson(
    session: AsyncSession,
    storage: Storage,
    *,
    admin_tg_id: int,
    caption: str | None,
    staged: Sequence[Path],
) -> Lesson:
    """Превратить черновик в урок.

    Оригиналы копируются в ``lessons/<id>/`` КАК ЕСТЬ, без метки: именно по ним
    потом выравнивается утёкшая картинка при трассировке.
    """
    if not staged:
        raise ValueError("нет ни одной картинки")

    def materialize(lesson_id: int) -> list[ImageSpec]:
        specs: list[ImageSpec] = []
        for position, source in enumerate(staged):
            target = storage.original(lesson_id, position)
            shutil.copyfile(source, target)
            width, height = image_size(target)
            specs.append(ImageSpec(path=target, width=width, height=height))
        return specs

    lesson = await create_lesson(
        session,
        admin_tg_id=admin_tg_id,
        caption=caption,
        materialize=materialize,
    )
    logger.info(
        "урок %d сохранён: %d картинок, автор %d", lesson.id, len(lesson.images), admin_tg_id
    )
    return lesson
