"""Формат полезной нагрузки водяного знака.

Метка имеет ФИКСИРОВАННУЮ длину: ``V|{uid:04d}|{lesson_id:05d}`` — 13 ASCII-символов.
Благодаря этому длина в битах (``WM_BIT_LENGTH``) — константа, и её не нужно
хранить рядом с каждой доставкой: при извлечении она известна заранее.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Версия формата — первый символ метки. Менять только вместе с ``WM_BIT_LENGTH``.
PAYLOAD_VERSION: Final[str] = "V"

#: Границы, накладываемые шириной полей формата.
MAX_UID: Final[int] = 9999
MAX_LESSON_ID: Final[int] = 99999

_PAYLOAD_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(PAYLOAD_VERSION)}\|(\d{{4}})\|(\d{{5}})$"
)


@dataclass(frozen=True, slots=True)
class Payload:
    """Что именно вшито в картинку: кому выдана и от какого урока."""

    uid: int
    lesson_id: int

    def __post_init__(self) -> None:
        if not 0 <= self.uid <= MAX_UID:
            raise ValueError(f"uid вне диапазона 0..{MAX_UID}: {self.uid}")
        if not 0 <= self.lesson_id <= MAX_LESSON_ID:
            raise ValueError(
                f"lesson_id вне диапазона 0..{MAX_LESSON_ID}: {self.lesson_id}"
            )

    def encode(self) -> str:
        return f"{PAYLOAD_VERSION}|{self.uid:04d}|{self.lesson_id:05d}"

    @classmethod
    def parse(cls, raw: str) -> Payload | None:
        """Разобрать извлечённую строку. ``None`` — если это мусор, а не метка.

        Строгий разбор здесь работает как самопроверка: при неудачном извлечении
        blind-watermark вернёт случайные байты, которые почти наверняка не пройдут
        регулярку. Это отсекает ложные «опознания».
        """
        match = _PAYLOAD_RE.match(raw.strip())
        if match is None:
            return None
        return cls(uid=int(match.group(1)), lesson_id=int(match.group(2)))

    def __str__(self) -> str:  # pragma: no cover - тривиально
        return self.encode()


def bit_length(payload: str) -> int:
    """Длина метки в битах ровно так, как её считает blind-watermark.

    Библиотека в режиме ``mode="str"`` делает ``bin(int(hex, 16))[2:]``, то есть
    отбрасывает ведущие нули старшего байта. Для нашего формата старший байт —
    всегда ``'V'`` (0x56), поэтому теряется ровно один бит и длина постоянна.
    """
    return len(bin(int(payload.encode("utf-8").hex(), base=16))[2:])


#: ``wm_shape`` для извлечения. Константа, вычисленная из формата, а не «магия».
WM_BIT_LENGTH: Final[int] = bit_length(Payload(uid=0, lesson_id=0).encode())
