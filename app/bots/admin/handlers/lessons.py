"""Создание урока и веерная рассылка меченых копий."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.admin.handlers.report import format_report
from app.bots.files import ImageRejected, download_image, extract_file_id
from app.config import Settings
from app.db.repo import list_active_students
from app.services import LessonBroadcaster, LessonSpec, Storage, save_lesson
from app.utils import CAPTION_LIMIT, split_text
from app.watermark import image_size
from app.watermark.text import MIN_GAPS
from app.watermark.text import fits as text_fits

logger = logging.getLogger(__name__)

router = Router(name="lessons")

SEND_CALLBACK = "newlesson:send"
CANCEL_CALLBACK = "newlesson:cancel"

#: Как часто обновлять сообщение с прогрессом рассылки.
_PROGRESS_EVERY = 5


class NewLesson(StatesGroup):
    collecting = State()
    confirming = State()


def _confirm_keyboard(recipients: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Разослать ({recipients})", callback_data=SEND_CALLBACK
                )
            ],
            [InlineKeyboardButton(text="Отменить", callback_data=CANCEL_CALLBACK)],
        ]
    )


async def _staged_paths(state: FSMContext) -> list[Path]:
    data = await state.get_data()
    return [Path(item) for item in data.get("images", [])]


@router.message(Command("newlesson"))
async def start_new_lesson(message: Message, state: FSMContext, storage: Storage) -> None:
    await state.clear()
    staging = storage.new_staging_dir()
    await state.set_state(NewLesson.collecting)
    await state.set_data({"staging": str(staging), "images": [], "caption": None})
    await message.answer(
        "<b>Новый урок</b>\n\n"
        "Пришлите картинку (или несколько) и текст поста.\n\n"
        "• Текст можно отправить подписью к картинке или отдельным сообщением.\n"
        "• Лучше слать картинки <b>файлом</b>, а не фото: так оригинал "
        "сохранится без пережатия.\n\n"
        "Когда закончите — /done. Отмена — /cancel."
    )


@router.message(NewLesson.collecting, F.photo | F.document)
async def collect_image(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    staging = Path(data["staging"])
    images: list[str] = list(data.get("images", []))

    try:
        file_id = extract_file_id(message)
        target = Storage.staged_original(staging, len(images))
        width, height = await download_image(bot, file_id, target)
    except ImageRejected as exc:
        await message.answer(f"Не принято: {exc}")
        return

    images.append(str(target))
    caption = data.get("caption")
    if caption is None and message.caption:
        caption = message.html_text

    await state.update_data(images=images, caption=caption)
    await message.answer(
        f"Картинка {len(images)} принята ({width}×{height}). "
        "Ещё? Или /done, чтобы продолжить."
    )


@router.message(NewLesson.collecting, F.text, ~F.text.startswith("/"))
async def collect_caption(message: Message, state: FSMContext) -> None:
    await state.update_data(caption=message.html_text)
    await message.answer("Текст поста сохранён. Добавьте картинки или /done.")


@router.message(NewLesson.collecting, Command("done"))
async def finish_collecting(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    data = await state.get_data()
    images = [Path(item) for item in data.get("images", [])]
    if not images:
        await message.answer("Ни одной картинки не прислано — метку некуда вшивать.")
        return

    students = await list_active_students(session)
    if not students:
        await message.answer(
            "Нет ни одного ученика, принявшего оферту. "
            "Урок можно сохранить, но рассылать некому."
        )

    caption: str | None = data.get("caption")
    await state.set_state(NewLesson.confirming)

    await message.answer("<b>Так это увидит ученик:</b>")
    await message.answer_photo(
        photo=FSInputFile(images[0]),
        caption=caption if caption and len(caption) <= CAPTION_LIMIT else None,
    )
    if caption and len(caption) > CAPTION_LIMIT:
        for chunk in split_text(caption):
            await message.answer(chunk)

    lines = [f"Картинок: {len(images)}", f"Зарегистрировано учеников: {len(students)}"]
    if settings.vsa_group_id is not None:
        # Точное число получателей известно только в момент рассылки: членство
        # сверяется тогда же. Обещать здесь «получателей N» было бы неправдой.
        lines.append("Кого уже нет в группе курса — пропустим, список будет в отчёте.")
    oversized = [path for path in images if max(image_size(path)) > settings.lesson_max_side]
    if oversized:
        # Не сюрприз, а осознанный компромисс — автор должен о нём знать.
        lines.append(
            f"\nДлинная сторона будет уменьшена до {settings.lesson_max_side} px: "
            "иначе метка не читается со скриншота чата."
        )
    lines.append("\nКаждый получит свою копию с личной меткой.")
    if caption:
        lines.append(
            "Текст тоже будет помечен." if text_fits(caption)
            else f"Текст останется без метки: в нём меньше {MIN_GAPS + 1} слов."
        )

    await message.answer("\n".join(lines), reply_markup=_confirm_keyboard(len(students)))


@router.callback_query(NewLesson.confirming, F.data == SEND_CALLBACK)
async def send_lesson(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    storage: Storage,
    broadcaster: LessonBroadcaster,
    settings: Settings,
) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return

    data = await state.get_data()
    staged = [Path(item) for item in data.get("images", [])]
    caption: str | None = data.get("caption")
    if not staged:
        await message.answer("Черновик потерян. Начните заново: /newlesson")
        await state.clear()
        return

    students = await list_active_students(session)
    lesson = await save_lesson(
        session,
        storage,
        admin_tg_id=callback.from_user.id,
        caption=caption,
        staged=staged,
        max_side=settings.lesson_max_side,
    )
    Storage.drop_staging(Path(data["staging"]))
    await state.clear()

    if not students:
        await message.answer(
            f"Урок #{lesson.id} сохранён, но получателей нет. "
            "Как только ученики зарегистрируются, урок можно переслать вручную."
        )
        return

    status = await message.answer(f"Рассылаю… 0/{len(students)}")
    last_shown = 0

    async def on_progress(done: int, total: int) -> None:
        nonlocal last_shown
        if done - last_shown < _PROGRESS_EVERY and done != total:
            return
        last_shown = done
        try:
            await status.edit_text(f"Рассылаю… {done}/{total}")
        except Exception:  # noqa: BLE001 - прогресс не важнее рассылки
            logger.debug("не удалось обновить прогресс", exc_info=True)

    report = await broadcaster.run(
        LessonSpec.of(lesson), students, on_progress=on_progress
    )

    await message.answer(format_report(f"Урок #{lesson.id} разослан", report))


@router.callback_query(NewLesson.confirming, F.data == CANCEL_CALLBACK)
async def cancel_from_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _cancel(state)
    if isinstance(callback.message, Message):
        await callback.message.answer("Черновик удалён.")


@router.message(StateFilter(NewLesson.collecting, NewLesson.confirming), Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await _cancel(state)
    await message.answer("Черновик удалён.")


async def _cancel(state: FSMContext) -> None:
    data = await state.get_data()
    staging = data.get("staging")
    if staging:
        Storage.drop_staging(Path(staging))
    await state.clear()
