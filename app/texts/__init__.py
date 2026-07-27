"""Тексты, которые меняются без правки кода.

Оферту финализирует юрист, поэтому она лежит отдельным файлом (путь задаётся
``OFFER_PATH`` в .env) и читается с диска. Разметка — HTML-подмножество Telegram
(``<b>``, ``<i>``, ``<u>``, ``<a href>``, ``<code>``), потому что боты работают
с ``parse_mode=HTML``.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FALLBACK_OFFER = (
    "<b>Условия доступа</b>\n\n"
    "Текст оферты не найден на диске. Обратитесь к администратору курса — "
    "без принятых условий доступ к материалам не выдаётся."
)


def load_offer(path: Path) -> str:
    """Прочитать текст оферты. Кэша нет намеренно: правку видно сразу."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("не удалось прочитать оферту %s: %s", path, exc)
        return _FALLBACK_OFFER
    return text or _FALLBACK_OFFER
