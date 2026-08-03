"""Метка в тексте: невидимые символы между словами.

Каждый пробел несёт два бита, закодированные одним из четырёх символов нулевой
ширины. Глазом они не видны, ширины не имеют, копипаст переживают.

Чего метка в тексте НЕ переживает — и это важно понимать:

* скриншот: там остаются пиксели, а не символы;
* перепечатку руками;
* редакторы и мессенджеры, вырезающие форматирующие символы.

Поэтому она дополняет метку в картинке, но не заменяет её.

Формат компактный, двоичный, а не текстовый как у картинки: в изображении метка
повторяется тысячи раз по всему полотну, а здесь мест ровно столько, сколько
пробелов, и каждый бит на счету.
"""

from __future__ import annotations

import re
from binascii import crc_hqx
from collections import Counter
from typing import Final

from app.watermark.payload import MAX_LESSON_ID, MAX_UID, Payload

#: Четыре символа нулевой ширины — по два бита на каждый пробел.
#: Все четыре относятся к категории Cf (format) и не имеют ширины.
_ALPHABET: Final[tuple[str, str, str, str]] = ("​", "‌", "‍", "⁠")
_CODE: Final[dict[str, str]] = {char: f"{i:02b}" for i, char in enumerate(_ALPHABET)}

#: Ширина полей. uid и lesson_id — ровно столько, сколько нужно под их пределы.
#:
#: Контрольная сумма широкая не из перестраховки: при чтении перебираются все
#: смещения, и на каждое приходится своя попытка угадать. С восьмибитной суммой
#: повреждённый текст выдавал ЧУЖОЙ номер примерно в одном случае из двухсот —
#: проверено стресс-тестом. Для инструмента, по которому назначают виноватого,
#: это неприемлемо: шестнадцать бит опускают вероятность в тысячи раз.
_MAGIC_BITS: Final[int] = 5
_UID_BITS: Final[int] = 14
_LESSON_BITS: Final[int] = 17
_CRC_BITS: Final[int] = 16
TEXT_BITS: Final[int] = _MAGIC_BITS + _UID_BITS + _LESSON_BITS + _CRC_BITS

#: Опознавательная последовательность. Без неё случайный набор символов
#: нулевой ширины (их иногда вставляют редакторы) читался бы как метка.
_MAGIC: Final[int] = 0b10110

#: Сколько пробелов нужно, чтобы метка влезла.
MIN_GAPS: Final[int] = (TEXT_BITS + 1) // 2

#: Разбор с учётом разметки: внутрь тегов вставлять нельзя, это сломало бы HTML.
_TOKENS: Final[re.Pattern[str]] = re.compile(r"<[^>]*>|\s+|[^<\s]+")


def _checksum(value: int) -> int:
    """CRC-16, а не самодельная свёртка: у той плохое распределение, и
    повреждённые данные слишком часто проходили проверку."""
    return crc_hqx(value.to_bytes((_UID_BITS + _LESSON_BITS + 7) // 8, "big"), 0)


def _to_bits(payload: Payload) -> str:
    if not 0 <= payload.uid <= MAX_UID:  # pragma: no cover - Payload уже проверил
        raise ValueError(f"uid вне диапазона: {payload.uid}")
    if not 0 <= payload.lesson_id <= MAX_LESSON_ID:  # pragma: no cover
        raise ValueError(f"lesson_id вне диапазона: {payload.lesson_id}")

    body = (payload.uid << _LESSON_BITS) | payload.lesson_id
    value = (_MAGIC << (_UID_BITS + _LESSON_BITS + _CRC_BITS)) | (body << _CRC_BITS)
    value |= _checksum(body)
    return format(value, f"0{TEXT_BITS}b")


def _from_bits(bits: str) -> Payload | None:
    if len(bits) != TEXT_BITS:
        return None
    value = int(bits, 2)

    crc = value & ((1 << _CRC_BITS) - 1)
    body = (value >> _CRC_BITS) & ((1 << (_UID_BITS + _LESSON_BITS)) - 1)
    magic = value >> (_UID_BITS + _LESSON_BITS + _CRC_BITS)

    if magic != _MAGIC or crc != _checksum(body):
        return None

    uid = body >> _LESSON_BITS
    lesson_id = body & ((1 << _LESSON_BITS) - 1)
    if uid > MAX_UID or lesson_id > MAX_LESSON_ID:
        return None
    try:
        return Payload(uid=uid, lesson_id=lesson_id)
    except ValueError:  # pragma: no cover - диапазоны уже проверены выше
        return None


def gaps(text: str) -> int:
    """Сколько пробелов пригодно под метку (внутри тегов не считаются)."""
    return sum(1 for token in _TOKENS.findall(text) if token.isspace())


def fits(text: str) -> bool:
    return gaps(text) >= MIN_GAPS


def has_marks(text: str) -> bool:
    """Есть ли в тексте хоть один наш невидимый символ.

    Дешёвая проверка перед разбором: без неё бот отвечал бы на любой текст.
    """
    return any(char in _CODE for char in text)


def strip_marks(text: str) -> str:
    """Убрать все символы нулевой ширины — текст как его видит человек."""
    return "".join(char for char in text if char not in _CODE)


def embed(text: str, payload: Payload) -> str | None:
    """Вшить метку. ``None`` — в тексте не хватает пробелов.

    Возвращаем именно ``None``, а не текст без метки: вызывающий должен знать,
    что материал ушёл непомеченным, и сказать об этом автору.
    """
    if not fits(text):
        return None

    bits = _to_bits(payload)
    pairs = [bits[i : i + 2].ljust(2, "0") for i in range(0, len(bits), 2)]

    out: list[str] = []
    index = 0
    for token in _TOKENS.findall(strip_marks(text)):
        out.append(token)
        if token.isspace():
            # Метка повторяется по кругу до конца текста — ровно как в картинке,
            # где она вписана циклически по всем блокам. Иначе она осела бы в
            # начале, и обрезанный сверху кусок нельзя было бы опознать.
            out.append(_ALPHABET[int(pairs[index % len(pairs)], 2)])
            index += 1
    return "".join(out)


def extract(text: str) -> Payload | None:
    """Достать метку. ``None`` — её нет, она повреждена или копии спорят.

    Метка повторяется циклически, поэтому читаются ВСЕ уцелевшие периоды, а не
    первый попавшийся. Если разные копии дали разные номера, значит повреждений
    столько, что верить нельзя ни одному, — и лучше честно промолчать, чем
    назвать чужого.
    """
    found = [_CODE[char] for char in text if char in _CODE]
    if len(found) < MIN_GAPS:
        return None

    stream = "".join(found)
    # Смещение кратно двум: один символ несёт ровно пару бит, и сдвиг на
    # нечётное число бит рассинхронизировал бы разбор.
    candidates = [
        payload
        for start in range(0, len(stream) - TEXT_BITS + 1, 2)
        if (payload := _from_bits(stream[start : start + TEXT_BITS])) is not None
    ]
    if not candidates:
        return None

    ranked = Counter(candidates).most_common(2)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None  # ничья между копиями — гадать нельзя
    return ranked[0][0]
