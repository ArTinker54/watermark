"""Самопроверка метки: связка с именами файлов персональных копий.

Проверяется не логика ответа, а стык: имя копии задаёт ``Storage``, а
самопроверка ищет копии шаблоном и достаёт из имени номер участника. Разъехаться
эти два места могут молча — и тогда самопроверка скажет «копий нет» там, где их
сотня, то есть соврёт ровно в том вопросе, ради которого её и написали.
"""

from __future__ import annotations

from pathlib import Path

from app.services import Storage
from app.services.selfcheck import uid_from_name
from app.watermark.engine import EMBED_VERSION


def test_imya_kopii_razbiraetsya_obratno(tmp_path: Path) -> None:
    """Номер участника обязан доставаться из имени, которое сделал Storage."""
    storage = Storage(root=tmp_path)

    path = storage.marked(lesson_id=141, uid=42, position=0)

    assert uid_from_name(path) == 42


def test_shablon_poiska_nahodit_kopii(tmp_path: Path) -> None:
    """Копии на диске должны находиться тем же шаблоном, что и в самопроверке."""
    storage = Storage(root=tmp_path)
    folder = storage.marked_root / "141"
    for uid in (1, 42, 1234):
        storage.marked(lesson_id=141, uid=uid, position=0).write_bytes(b"png")

    found = sorted(folder.glob(f"*_v{EMBED_VERSION}.png"))

    assert [uid_from_name(item) for item in found] == [1, 42, 1234]


def test_kopiya_proshloy_versii_ne_schitaetsya_svezhey(tmp_path: Path) -> None:
    """Копия, сделанная прошлой процедурой встраивания, не должна попадать в выборку.

    Иначе самопроверка вскроет заведомо нечитаемый файл и объявит поломкой то,
    что поломкой не является.
    """
    storage = Storage(root=tmp_path)
    folder = storage.marked_root / "141"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"0042_0_v{EMBED_VERSION - 1}.png").write_bytes(b"png")
    storage.marked(lesson_id=141, uid=7, position=0).write_bytes(b"png")

    found = sorted(folder.glob(f"*_v{EMBED_VERSION}.png"))

    assert [uid_from_name(item) for item in found] == [7]


def test_chuzhoe_imya_ne_ronyaet_razbor(tmp_path: Path) -> None:
    """В папке может лежать что угодно — это не повод падать."""
    assert uid_from_name(Path("мусор.png")) is None
