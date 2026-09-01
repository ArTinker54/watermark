"""Бот в личке автора: меню частых вопросов и вызов живого человека.

Работает через Telegram Business: бот подключён к личному аккаунту автора и
отвечает от её имени тем, кому она разрешила в настройках (в нашем случае —
«не контакты»). Отсюда две особенности, которых нет у обычного бота.

Первая: отвечать надо с ``business_connection_id``, иначе сообщение уйдёт от
имени бота в его собственный чат, а не в переписку автора.

Вторая: в этот же обработчик приходят и сообщения самой автора — Telegram не
делит входящие и исходящие. Их надо узнавать, потому что это единственный
сигнал «человек вступил в разговор, боту пора замолчать».
"""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.dm import dialog, faq
from app.bots.dm.dialog import Reply

logger = logging.getLogger(__name__)

router = Router(name="dm")

#: Префикс callback_data. Без него нельзя отличить наши кнопки от чужих,
#: если в этом же боте когда-нибудь появится что-то ещё.
_PREFIX = "faq"

#: Ссылки в ответах ведут в каналы автора, и Telegram разворачивает первую
#: из них в карточку на пол-экрана. Меню под таким предпросмотром уезжает
#: за край, поэтому превью гасим.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

#: Кого уведомлять о нажатии «Позвать Алёну». Узнаём из подключения — оно
#: приходит и при старте бота, если бот уже подключён к аккаунту.
_owner: dict[str, int] = {}


def menu() -> InlineKeyboardMarkup:
    """Меню вопросов. По кнопке в строке: формулировки длинные, в два не влезают."""
    rows = [
        [
            InlineKeyboardButton(
                text=answer.button, callback_data=f"{_PREFIX}:{answer.key}"
            )
        ]
        for answer in faq.FAQ
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=faq.CALL_HUMAN_BUTTON, callback_data=f"{_PREFIX}:{faq.CALL_HUMAN}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def after_answer() -> InlineKeyboardMarkup:
    """Клавиатура под готовым ответом: назад к списку и вызов человека.

    Полное меню тут не годится. Оно длиннее любого ответа, и человек, открывший
    один вопрос, получал экран, где сам ответ уехал вверх, а под ним снова
    десять кнопок — будто ничего и не выбирал.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=faq.BACK_BUTTON, callback_data=f"{_PREFIX}:{faq.BACK}")],
            [
                InlineKeyboardButton(
                    text=faq.CALL_HUMAN_BUTTON,
                    callback_data=f"{_PREFIX}:{faq.CALL_HUMAN}",
                )
            ],
        ]
    )


def card(user: User | None, viewed: list[str]) -> str:
    """Карточка для автора: кто спрашивает и что уже успел прочитать.

    Список открытых ответов важнее, чем кажется: он показывает, о чём человек
    уже знает, и избавляет от повторного пересказа того же самого.
    """
    name = escape(user.full_name) if user else "неизвестно кто"
    lines = [f"📞 <b>{name}</b> просит ответить лично"]
    if user is not None:
        handle = f"@{user.username}" if user.username else "без ника"
        lines.append(f'{escape(handle)} · <a href="tg://user?id={user.id}">открыть чат</a>')
    if viewed:
        titles = [faq.BY_KEY[key].button for key in viewed if key in faq.BY_KEY]
        if titles:
            lines.append("\nУже прочитал(а): " + "; ".join(titles))
    else:
        lines.append("\nМеню не открывал(а) — вопрос сразу к вам.")
    return "\n".join(lines)


async def _say(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    connection_id: str | None,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Сказать в переписку от имени автора (или от своего, если бота открыли напрямую)."""
    await bot.send_message(
        chat_id,
        text,
        business_connection_id=connection_id,
        reply_markup=keyboard,
        link_preview_options=_NO_PREVIEW,
    )


@router.business_connection()
async def remember_owner(event: BusinessConnection) -> None:
    """Запомнить, чей это аккаунт, — чтобы было кому пересылать вызовы."""
    _owner["id"] = event.user.id
    logger.info(
        "business-подключение %s: аккаунт %s, включено=%s",
        event.id,
        event.user.id,
        event.is_enabled,
    )


def from_owner_side(message: Message) -> bool:
    """Сообщение написано с той стороны переписки, а не собеседником.

    Проверка отдельной функцией, а не магическим фильтром: ``F`` умеет
    сравнивать поле со значением, но не два поля события между собой —
    ``F.from_user.id != F.chat.id`` было бы истиной всегда.
    """
    return message.from_user is not None and message.from_user.id != message.chat.id


@router.business_message(from_owner_side)
async def owner_replied(message: Message, session: AsyncSession) -> None:
    """Сообщение с той стороны переписки — то есть от автора или от нас самих.

    В business-апдейтах входящие и исходящие приходят одинаково. Отличаем по
    простому признаку: у входящего отправитель и чат — один и тот же человек,
    у исходящего чат остаётся собеседником, а отправителем становится автор.
    Свои же сообщения узнаём по ``sender_business_bot`` и на них не реагируем,
    иначе бот замолчал бы сразу после собственного приветствия.
    """
    if message.sender_business_bot is not None:
        return
    if message.is_from_offline:
        # Автоответ Telegram «меня нет на месте» — это не живой человек.
        return
    if message.business_connection_id is None:
        return
    if message.from_user is not None:
        _owner.setdefault("id", message.from_user.id)
    await dialog.mute(session, message.chat.id, message.business_connection_id)
    logger.info("автор ответила сама, бот молчит в чате %s", message.chat.id)


@router.business_message()
async def incoming(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Входящее от человека, которого нет в адресной книге автора."""
    connection_id = message.business_connection_id
    if connection_id is None:
        return

    record = await dialog.touch(session, message.chat.id, connection_id)
    verdict = dialog.decide(record)
    if verdict is Reply.SILENT:
        return

    text = faq.GREETING if verdict is Reply.GREET else faq.NUDGE
    await _say(bot, message.chat.id, text, connection_id=connection_id, keyboard=menu())
    if verdict is Reply.GREET:
        await dialog.mark_greeted(session, record)
    else:
        await dialog.mark_nudged(session, record)


@router.message(CommandStart())
async def direct_start(message: Message) -> None:
    """Бота открыли напрямую, а не через переписку с автором.

    Так делает и сама автор — ей это нужно, чтобы получать вызовы. Отвечаем тем
    же меню: скрывать в нём нечего, а объяснять человеку, что он «зашёл не с той
    стороны», незачем.
    """
    await message.answer(faq.GREETING, reply_markup=menu())


@router.callback_query(F.data.startswith(f"{_PREFIX}:"))
async def show_answer(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    """Нажата кнопка меню."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    key = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id
    # У CallbackQuery своего business_connection_id нет — он лежит на сообщении,
    # под которым нажали кнопку.
    connection_id = callback.message.business_connection_id

    if key == faq.CALL_HUMAN:
        await _call_human(callback, session, bot, chat_id, connection_id)
        return

    if key == faq.BACK:
        await callback.answer()
        await _say(
            bot, chat_id, faq.MENU_AGAIN, connection_id=connection_id, keyboard=menu()
        )
        return

    answer = faq.BY_KEY.get(key)
    if answer is None:
        await callback.answer("Этого ответа больше нет в списке", show_alert=True)
        return

    await callback.answer()
    await _say(
        bot, chat_id, answer.body(), connection_id=connection_id, keyboard=after_answer()
    )
    record = await dialog.touch(session, chat_id, connection_id or "")
    await dialog.mark_viewed(session, record, key)


async def _call_human(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    chat_id: int,
    connection_id: str | None,
) -> None:
    """«Позвать Алёну»: подтвердить человеку и дёрнуть автора карточкой."""
    record = await dialog.touch(session, chat_id, connection_id or "")
    if not dialog.may_call(record):
        await callback.answer("Уже передала — Алёна ответит здесь же.", show_alert=True)
        return

    await callback.answer()
    await _say(bot, chat_id, faq.CALL_HUMAN_REPLY, connection_id=connection_id)
    await dialog.mark_called(session, record)

    owner_id = _owner.get("id")
    if owner_id is None:
        # Не потеря: переписка и так лежит в личке автора, она её увидит.
        # Но карточку послать некуда, и об этом лучше знать из логов.
        logger.warning("некому передать вызов: business-подключение ещё не приходило")
        return
    try:
        await bot.send_message(
            owner_id, card(callback.from_user, dialog.viewed_keys(record))
        )
    except TelegramAPIError as exc:
        logger.warning("не удалось передать вызов автору: %s", exc)


def build_router() -> Router:
    return router
