"""Метка в тексте: что она переживает и где сдаётся."""

from __future__ import annotations

import html
import json
import unicodedata

import pytest

from app.watermark import Payload
from app.watermark.text import (
    MIN_GAPS,
    TEXT_BITS,
    embed,
    extract,
    fits,
    gaps,
    strip_marks,
)

PAYLOAD = Payload(uid=42, lesson_id=7)

SHORT = "Смотри на объём в момент пробоя"

#: Текста хватает ровно на одну метку, но не на две.
ONE_PERIOD = (
    "Разбор ситуации по газу. Первое: уровень 2.81 держится третий день, "
    "каждый подход к нему сопровождается ростом объёма, но цена не идёт выше. "
    "Второе: спред на последних барах сузился, продавец разгружается в "
    "покупателя. Вход только после подтверждения, стоп за уровень."
)

#: Полноценный разбор: метка укладывается больше двух раз подряд.
LONG = ONE_PERIOD + (
    " Отдельно про объёмы: вчерашний всплеск пришёлся ровно на подход к уровню, "
    "и это уже третий такой подход за неделю. Если продавец действительно "
    "разгружается, следующий тест должен пройти на заметно меньшем объёме. "
    "Тогда и работаем, а до тех пор наблюдаем со стороны и не лезем."
)


def test_roundtrip() -> None:
    marked = embed(LONG, PAYLOAD)
    assert marked is not None
    assert extract(marked) == PAYLOAD


def test_mark_is_invisible() -> None:
    """Читатель не должен заметить ничего: видимый текст обязан совпасть."""
    marked = embed(LONG, PAYLOAD)
    assert marked is not None
    assert strip_marks(marked) == LONG
    assert len(marked) > len(LONG)


def test_short_text_is_refused_not_silently_skipped() -> None:
    """Короткая подпись метку не вмещает, и об этом надо знать явно."""
    assert gaps(SHORT) < MIN_GAPS
    assert not fits(SHORT)
    assert embed(SHORT, PAYLOAD) is None


def test_capacity_is_documented_in_words() -> None:
    """Порог держим на виду: он определяет, какие подписи вообще метятся."""
    assert TEXT_BITS % 2 == 0, "два бита на пробел — TEXT_BITS обязан быть чётным"
    assert MIN_GAPS == TEXT_BITS // 2
    words = " ".join(["слово"] * (MIN_GAPS + 1))
    assert fits(words)
    assert not fits(" ".join(["слово"] * MIN_GAPS))


@pytest.mark.parametrize(
    ("name", "attack"),
    [
        ("копипаст", lambda s: s),
        ("HTML-экранирование", lambda s: html.unescape(html.escape(s))),
        ("JSON", lambda s: json.loads(json.dumps(s))),
        ("нормализация NFKC", lambda s: unicodedata.normalize("NFKC", s)),
        ("схлопнули пробелы", lambda s: s),
        ("обрезали начало", lambda s: s[len(s) // 4 :]),
        ("дописали текст вокруг", lambda s: "Смотрите что скинули: " + s + " — вот так."),
    ],
)
def test_survives(name: str, attack) -> None:  # noqa: ANN001
    marked = embed(LONG, PAYLOAD)
    assert marked is not None
    assert extract(attack(marked)) == PAYLOAD, f"метка потеряна: {name}"


@pytest.mark.parametrize(
    ("name", "attack"),
    [
        ("перепечатали руками", strip_marks),
        ("скриншот (остались пиксели)", strip_marks),
    ],
)
def test_known_limits(name: str, attack) -> None:  # noqa: ANN001
    """Границы зафиксированы: об этих случаях мы знаем и не обещаем лишнего."""
    marked = embed(LONG, PAYLOAD)
    assert marked is not None
    assert extract(attack(marked)) is None, f"неожиданно выжила: {name}"


def test_repetition_is_what_survives_cutting() -> None:
    """Обрезку переживает повторяемость, а не сама метка.

    Метка укладывается периодом. Если текста хватило ровно на один период,
    обрезанное начало уносит его целиком и опознать нечего. Со второго периода
    появляется запас. Это и есть настоящая граница, и обещать больше нельзя.
    """
    assert gaps(ONE_PERIOD) >= MIN_GAPS
    assert gaps(ONE_PERIOD) < MIN_GAPS * 2
    assert gaps(LONG) >= MIN_GAPS * 2

    scarce = embed(ONE_PERIOD, PAYLOAD)
    plenty = embed(LONG, PAYLOAD)
    assert scarce is not None and plenty is not None

    assert extract(scarce) == PAYLOAD, "целый текст читается в любом случае"
    assert extract(scarce[len(scarce) // 4 :]) is None
    assert extract(plenty[len(plenty) // 4 :]) == PAYLOAD


def test_clean_text_has_no_mark() -> None:
    """Обычный текст не должен опознаваться как меченый."""
    assert extract(LONG) is None
    assert extract("") is None
    assert extract("﻿текст с посторонним невидимым символом") is None


def test_small_damage_is_repaired_by_repetition() -> None:
    """Один потерянный символ портит период, но остальные копии целы."""
    from app.watermark.text import _ALPHABET

    marked = embed(LONG, PAYLOAD)
    assert marked is not None
    assert extract(marked.replace(_ALPHABET[0], "", 1)) == PAYLOAD


def test_damage_never_yields_someone_elses_number() -> None:
    """Главное требование: не прочитать чужого.

    Ошибиться номером хуже, чем не прочитать вовсе: по метке назначают
    виноватого. Поэтому при любых повреждениях допустимы только два исхода —
    верный номер или честное «не читается».
    """
    import random

    from app.watermark.text import _ALPHABET

    marked = embed(LONG, PAYLOAD)
    assert marked is not None
    rng = random.Random(20260803)

    wrong = 0
    recovered = 0
    for _ in range(4000):
        damage = rng.choice((0.1, 0.3, 0.6, 0.9))
        chars = list(marked)
        for position, char in enumerate(chars):
            if char in _ALPHABET and rng.random() < damage:
                chars[position] = rng.choice([*_ALPHABET, ""])
        got = extract("".join(chars))
        if got == PAYLOAD:
            recovered += 1
        elif got is not None:
            wrong += 1

    assert wrong == 0, f"выдано чужих номеров: {wrong} из 4000"
    # Заодно убеждаемся, что защита не выродилась в «всегда молчим».
    assert recovered > 0, "при лёгких повреждениях метка обязана читаться"


@pytest.mark.parametrize(
    ("uid", "lesson_id"),
    [(0, 0), (1, 1), (42, 7), (9999, 99999), (5000, 50000)],
)
def test_all_ranges(uid: int, lesson_id: int) -> None:
    payload = Payload(uid=uid, lesson_id=lesson_id)
    marked = embed(LONG, payload)
    assert marked is not None
    assert extract(marked) == payload


def test_html_markup_is_not_broken() -> None:
    """Внутрь тегов вставлять нельзя — разметка перестанет работать."""
    text = (
        "<b>Разбор ситуации</b> по газу за сегодня. Первое: уровень держится "
        "третий день подряд, каждый подход <i>сопровождается ростом объёма</i>, "
        "а цена всё равно не идёт выше этой отметки. Вход только после "
        "подтверждения, стоп ставим за уровень."
    )
    marked = embed(text, PAYLOAD)
    assert marked is not None
    assert extract(marked) == PAYLOAD

    # Теги целы: ни одного невидимого символа внутри угловых скобок.
    # Внутри самого <b>...</b> метка стоять может — на разметку это не влияет.
    import re

    for tag in re.findall(r"<[^>]*>", marked):
        assert strip_marks(tag) == tag, f"метка попала внутрь тега: {tag!r}"
    assert re.findall(r"<[^>]*>", marked) == re.findall(r"<[^>]*>", text)
    assert strip_marks(marked) == text


def test_two_students_get_different_text() -> None:
    first = embed(LONG, Payload(uid=1, lesson_id=7))
    second = embed(LONG, Payload(uid=2, lesson_id=7))
    assert first is not None and second is not None
    assert first != second
    assert strip_marks(first) == strip_marks(second) == LONG
