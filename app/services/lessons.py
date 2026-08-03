"""Сборка урока из черновика: перенос пристинных оригиналов в постоянное место."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Lesson
from app.db.repo import ImageSpec, create_lesson
from app.services.storage import Storage

logger = logging.getLogger(__name__)


def fit_to_max_side(source: Path, target: Path, max_side: int) -> tuple[int, int]:
    """Скопировать картинку, ограничив длинную сторону. Возвращает (width, height).

    Уменьшение здесь не про экономию места, а про читаемость метки. Telegram
    показывает фото в ленте уменьшенным независимо от его размера: вертикальный
    скрин 589x1280 виден как 225x489, то есть на 38%. Метка столько не переживает.
    Если хранить оригинал меньшего размера, ТА ЖЕ картинка в ленте занимает уже
    большую его долю — и со скриншота чата метка читается.

    Картинку меньше потолка не трогаем: увеличивать нечего.
    """
    with Image.open(source) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        scale = min(1.0, max_side / max(width, height))
        if scale < 1.0:
            width, height = round(width * scale), round(height * scale)
            rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        rgb.save(target, format="PNG")
    return width, height


async def save_lesson(
    session: AsyncSession,
    storage: Storage,
    *,
    admin_tg_id: int,
    caption: str | None,
    staged: Sequence[Path],
    max_side: int,
    question_id: int | None = None,
    video_file_id: str | None = None,
    video_kind: str | None = None,
    course_id: int | None = None,
) -> Lesson:
    """Превратить черновик в урок.

    Оригиналы кладутся в ``lessons/<id>/`` БЕЗ метки: именно по ним потом
    выравнивается утёкшая картинка при трассировке. Единственное изменение —
    ограничение длинной стороны, см. :func:`fit_to_max_side`.

    Материал без картинок допустим: объявление из одного текста метку несёт
    само. А вот пустой материал — нет, отправлять было бы нечего.
    """
    if not staged and not caption and video_file_id is None:
        raise ValueError("материал без картинок, текста и видео отправлять нечего")

    def materialize(lesson_id: int) -> list[ImageSpec]:
        specs: list[ImageSpec] = []
        for position, source in enumerate(staged):
            target = storage.original(lesson_id, position)
            width, height = fit_to_max_side(source, target, max_side)
            specs.append(ImageSpec(path=target, width=width, height=height))
        return specs

    lesson = await create_lesson(
        session,
        admin_tg_id=admin_tg_id,
        caption=caption,
        materialize=materialize,
        question_id=question_id,
        video_file_id=video_file_id,
        video_kind=video_kind,
        course_id=course_id,
    )
    sizes = ", ".join(f"{image.width}x{image.height}" for image in lesson.images)
    logger.info(
        "урок %d сохранён: %d картинок (%s), автор %d",
        lesson.id,
        len(lesson.images),
        sizes,
        admin_tg_id,
    )
    return lesson
