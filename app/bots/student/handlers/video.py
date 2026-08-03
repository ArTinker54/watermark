"""Приём видео от автора курса.

Видео принимает именно student-бот, а не admin-бот, и это не прихоть:
идентификатор файла в Telegram привязан к конкретному боту. Видео, загруженное
в admin-бота, отправить через student-бота невозможно.

Зато при таком приёме файл никуда не скачивается — хранится только его
идентификатор. Лимит Bot API в 20 МБ на скачивание не действует вовсе, и
размер ограничен лишь тем, что Telegram разрешает загрузить самому автору.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repo import save_uploaded_video

logger = logging.getLogger(__name__)

router = Router(name="video")
router.message.filter(F.chat.type == "private")


@router.message(F.video | F.document.mime_type.startswith("video/"))
async def accept_video(message: Message, session: AsyncSession, settings: Settings) -> None:
    """Принять видео от автора. Ученикам этот путь недоступен."""
    user = message.from_user
    if user is None or user.id not in settings.admin_id_set:
        # Не автор — пусть это уедет обычным вопросом, как любое сообщение.
        await message.answer(
            "Видео принимаются только от автора курса. "
            "Если это вопрос — напишите его текстом."
        )
        return

    # Тип запоминаем: сжатое видео и присланное файлом — разные сущности для
    # Telegram, и отправлять их надо разными методами.
    if message.video is not None:
        media, kind = message.video, "video"
    elif message.document is not None:
        media, kind = message.document, "document"  # type: ignore[assignment]
    else:  # pragma: no cover - фильтр уже отсеял
        return

    video = await save_uploaded_video(
        session,
        admin_tg_id=user.id,
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
        kind=kind,
        file_name=getattr(media, "file_name", None),
        file_size=media.file_size,
        duration=getattr(media, "duration", None),
    )
    logger.info(
        "видео принято от %s: %s (%s байт)", user.id, video.file_unique_id, video.file_size
    )

    how = "как видео" if kind == "video" else "файлом, без пережатия"
    await message.answer(
        f"<b>Видео принято</b> ({how})\n{video.summary}\n\n"
        "Вернитесь в бот автора, соберите урок командой /newlesson и нажмите "
        "/done — видео прицепится само.\n\n"
        "⚠️ В видео метки нет: определить, кто его слил, будет нельзя. "
        "Метку несёт только подпись, если она от 27 слов."
    )
