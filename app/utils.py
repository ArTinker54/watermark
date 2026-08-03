"""Мелкие общие помощники."""

from __future__ import annotations

import re
from datetime import datetime

#: Лимиты Telegram.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

#: Тег целиком: открывающий или закрывающий, вместе с атрибутами.
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*>")


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Разрезать текст на куски, влезающие в одно сообщение.

    Режем по границам абзацев и строк, чтобы не рвать слова посередине;
    если абзац сам длиннее лимита — режем жёстко.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def split_html(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Разрезать размеченный текст на сообщения, не сломав разметку.

    Подпись к материалу — это HTML: автор жмёт жирный, курсив, вставляет ссылки,
    а к тексту потом добавляются невидимые символы метки. Резать такую строку
    ровно по лимиту нельзя дважды: разрез посреди ``<b>`` даёт битый тег, а
    разрез между ``<b>`` и ``</b>`` — незакрытый. Telegram в обоих случаях
    отвергает сообщение целиком, и материал не доходит вообще ни до кого.

    Поэтому здесь: внутри тега не режем никогда, а открытые теги закрываем на
    границе куска и заново открываем в следующем.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    opened: list[str] = []  # открытые теги как в исходнике, с атрибутами
    names: list[str] = []  # их имена, для закрытия
    current: list[str] = []
    used = 0
    has_text = False  # есть ли в текущем куске хоть что-то, кроме тегов

    def tail() -> int:
        """Сколько места надо оставить под закрывающие теги."""
        return sum(len(name) + 3 for name in names)

    def flush() -> None:
        """Закрыть кусок. Пустышку из одних тегов не выпускаем: её отвергнут."""
        nonlocal used, has_text
        if not has_text:
            return
        chunks.append("".join(current) + "".join(f"</{name}>" for name in reversed(names)))
        current.clear()
        # Следующий кусок начинается с тех же тегов: разметка не теряется.
        current.extend(opened)
        used = sum(len(item) for item in opened)
        has_text = False

    def put(piece: str) -> None:
        nonlocal used
        current.append(piece)
        used += len(piece)

    def put_text(part: str) -> None:
        nonlocal has_text
        rest = part
        while rest:
            room = limit - used - tail()
            if len(rest) <= room:
                put(rest)
                has_text = has_text or bool(rest.strip())
                return
            if room <= 0:
                if not has_text:
                    # Одна разметка уже не влезает в лимит — резать дальше
                    # нечего, отдаём как есть, лишь бы не зациклиться.
                    put(rest)
                    has_text = True
                    return
                flush()
                continue
            window = rest[:room]
            # Ищем границу по-человечески: абзац, строка, слово. Если её нет
            # (сплошное полотно без пробелов) — режем по месту.
            cut = window.rfind("\n\n")
            if cut <= 0:
                cut = window.rfind("\n")
            if cut <= 0:
                cut = window.rfind(" ")
            if cut <= 0:
                cut = room
            put(rest[:cut])
            has_text = has_text or bool(rest[:cut].strip())
            rest = rest[cut:]
            flush()

    position = 0
    for match in _TAG.finditer(text):
        put_text(text[position : match.start()])
        tag = match.group(0)
        if len(tag) + tail() > limit - used:
            flush()
        put(tag)
        name = match.group(2).lower()
        if match.group(1):
            if name in names:
                # Закрываем последний одноимённый: разметка от автора бывает
                # кривой, и ронять из-за этого рассылку незачем.
                index = len(names) - 1 - names[::-1].index(name)
                names.pop(index)
                opened.pop(index)
        else:
            names.append(name)
            opened.append(tag)
        position = match.end()

    put_text(text[position:])
    flush()
    return chunks or [text]


def format_dt(value: datetime | None) -> str:
    """Дата для показа админу. В базе всё в UTC (см. app.db.models)."""
    return value.strftime("%d.%m.%Y %H:%M UTC") if value else "—"
