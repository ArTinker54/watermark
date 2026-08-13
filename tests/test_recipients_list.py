"""Списки в админке: показываем всех и не ломаемся об имена.

Оба случая пришли из жизни. Первый: у курса больше сотни учеников, команда
показывала сорок имён и «…и ещё 65» — найти конкретного человека было нельзя,
хотя команда нужна ровно для этого. Второй: имя из Telegram попадало в HTML как
есть, и один амперсанд делал недоступным весь список, а не одну строку.
"""

from __future__ import annotations

from app.bots.admin.handlers.recipients import _CHUNK, named, pack
from app.db import Student


def student(uid: int, full_name: str, username: str | None = None) -> Student:
    return Student(
        tg_user_id=1000 + uid, uid=uid, username=username, full_name=full_name
    )


def test_realnyy_kurs_razlozhen_i_nikto_ne_poteryan() -> None:
    """Сто пять человек с именами и никами — как в живой выдаче /course.

    Раньше здесь показывались сорок имён и «…и ещё 65». Теперь список уезжает в
    несколько сообщений, но целиком.
    """
    lines = [
        named(student(i, f"Семушин Геннадий {i}", username=f"Gennadii{i}"))
        for i in range(1, 106)
    ]

    parts = pack(lines)

    assert len(parts) > 1, "такой список обязан разложиться на несколько сообщений"
    restored = "\n".join(parts).split("\n")
    assert restored == lines


def test_korotkiy_spisok_ostayotsya_odnim_soobscheniem() -> None:
    """Дробить, когда всё влезает, — только мусорить в чате."""
    lines = [named(student(i, f"Ученик {i}")) for i in range(1, 40)]

    assert len(pack(lines)) == 1


def test_kazhdoe_soobschenie_vlezaet_v_limit() -> None:
    """Ни один кусок не должен упереться в потолок Telegram."""
    lines = [named(student(i, "Длинное имя ученика" * 5)) for i in range(1, 200)]

    for part in pack(lines):
        assert len(part) <= _CHUNK + max(len(line) for line in lines)
        assert len(part) < 4096


def test_stroka_ne_rvetsya_popolam() -> None:
    """Резать можно только между строками, иначе имя разъедет на два сообщения."""
    lines = [named(student(i, f"Ученик {i}")) for i in range(1, 300)]

    for part in pack(lines):
        for line in part.split("\n"):
            assert line in lines


def test_ampersand_v_imeni_ne_lomaet_razmetku() -> None:
    """«Рога & Копыта» — валидный ник, и он не должен ронять всё сообщение."""
    line = named(student(7, "Рога & Копыта", username="roga"))

    assert "&amp;" in line
    assert "& " not in line


def test_ugolki_v_imeni_ekraniruyutsya() -> None:
    """Имя вида <b> не должно превращаться в разметку."""
    line = named(student(8, "<b>жирный</b>"))

    assert "<b>" not in line
    assert "&lt;b&gt;" in line


def test_pustoy_spisok_daet_odno_soobschenie() -> None:
    """Пустота — это одно пустое сообщение, а не ноль и не падение."""
    assert pack([]) == [""]
