"""Формат метки: фиксированная длина и строгий разбор."""

from __future__ import annotations

import pytest

from app.watermark.payload import (
    MAX_LESSON_ID,
    MAX_UID,
    WM_BIT_LENGTH,
    Payload,
    bit_length,
)


def test_encode_is_zero_padded() -> None:
    assert Payload(uid=42, lesson_id=1).encode() == "V|0042|00001"


@pytest.mark.parametrize(
    ("uid", "lesson_id"),
    [(0, 0), (1, 1), (42, 7), (MAX_UID, MAX_LESSON_ID)],
)
def test_roundtrip(uid: int, lesson_id: int) -> None:
    payload = Payload(uid=uid, lesson_id=lesson_id)
    assert Payload.parse(payload.encode()) == payload


def test_length_is_constant_for_any_pair() -> None:
    """Ради этого и введены ведущие нули: wm_shape не зависит от значений."""
    lengths = {
        bit_length(Payload(uid=uid, lesson_id=lesson_id).encode())
        for uid in (0, 7, 999, MAX_UID)
        for lesson_id in (0, 42, MAX_LESSON_ID)
    }
    assert lengths == {WM_BIT_LENGTH}


@pytest.mark.parametrize(
    "raw",
    ["", "V|42|1", "X|0042|00001", "V|0042|00001|extra", "V|004a|00001", "��"],
)
def test_parse_rejects_garbage(raw: str) -> None:
    """Строгий разбор — защита от ложных опознаний на неудачном извлечении."""
    assert Payload.parse(raw) is None


@pytest.mark.parametrize(("uid", "lesson_id"), [(-1, 0), (MAX_UID + 1, 0), (0, -1)])
def test_out_of_range_rejected(uid: int, lesson_id: int) -> None:
    with pytest.raises(ValueError):
        Payload(uid=uid, lesson_id=lesson_id)
