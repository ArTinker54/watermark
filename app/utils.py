"""Мелкие общие помощники."""

from __future__ import annotations

from datetime import datetime

#: Лимиты Telegram.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


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


def format_dt(value: datetime | None) -> str:
    """Дата для показа админу. В базе всё в UTC (см. app.db.models)."""
    return value.strftime("%d.%m.%Y %H:%M UTC") if value else "—"
