"""Приём картинок из Telegram."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

#: Лимит Bot API на скачивание файла.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class ImageRejected(Exception):
    """Присланное не является пригодной картинкой."""


def extract_file_id(message: Message) -> str:
    """file_id картинки из сообщения — из photo или из image-документа.

    Документ предпочтительнее: фото Telegram уже пережал, а документ приходит
    байт в байт. Для оригинала урока это заметная разница в качестве.
    """
    if message.document is not None:
        mime = message.document.mime_type or ""
        if not mime.startswith("image/"):
            raise ImageRejected(f"это не картинка, а {mime or 'файл неизвестного типа'}")
        if (message.document.file_size or 0) > MAX_DOWNLOAD_BYTES:
            raise ImageRejected("файл больше 20 МБ — Telegram не отдаст его боту")
        return message.document.file_id
    if message.photo:
        return message.photo[-1].file_id  # последний размер — самый крупный
    raise ImageRejected("в сообщении нет картинки")


async def download_image(bot: Bot, file_id: str, target: Path) -> tuple[int, int]:
    """Скачать и сохранить в PNG. Возвращает (width, height).

    Всё приводится к PNG сразу: дальше картинка живёт как пристинный оригинал,
    и повторное сжатие ей ни к чему.
    """
    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    buffer.seek(0)

    try:
        with Image.open(buffer) as image:
            rgb = image.convert("RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            rgb.save(target, format="PNG")
            return rgb.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageRejected(f"не удалось прочитать картинку: {exc}") from exc
