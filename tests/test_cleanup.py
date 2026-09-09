"""Уборка тома: что можно удалять, а что нельзя ни при каких условиях.

Появилось после боевой поломки: том кончился, и бот перестал принимать
картинки — от автора и от учеников одинаково. Ошибка при этом звучала как
«не удалось прочитать картинку», то есть винила файл, а не диск.

Цена ошибки здесь несимметрична. Лишняя копия — это занятое место, а
удалённый оригинал урока — навсегда потерянная возможность найти источник
утечки: именно по оригиналам ищется область урока на присланной картинке.
Поэтому главный тест тут не про освобождённые байты, а про то, что оригиналы
уборка не трогает.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.services import Storage
from app.services.cleanup import human, purge_marked, purge_staging, usage


def filled(root: Path) -> Storage:
    storage = Storage(root=root)
    for lesson in (1, 2):
        storage.original(lesson, 0).write_bytes(b"O" * 1000)
        for uid in (1, 42, 195):
            storage.marked(lesson, uid, 0).write_bytes(b"M" * 500)
    storage.trace_input().write_bytes(b"T" * 300)
    (storage.questions_root / "q.png").write_bytes(b"Q" * 200)
    return storage


def test_originaly_urokov_ubroka_ne_trogaet(tmp_path: Path) -> None:
    """Главная гарантия. Оригинал восстановить неоткуда, копию — всегда."""
    storage = filled(tmp_path)
    originals = sorted(storage.lessons_root.rglob("*.png"))

    purge_marked(storage)
    purge_staging(storage, older_than=timedelta())

    assert sorted(storage.lessons_root.rglob("*.png")) == originals
    assert all(path.read_bytes() for path in originals)


def test_prislannoe_lyudmi_tozhe_ostaetsya(tmp_path: Path) -> None:
    """Трассировки и вопросы — доказательная база и переписка, не мусор."""
    storage = filled(tmp_path)

    purge_marked(storage)

    assert list(storage.traces_root.rglob("*.png"))
    assert list(storage.questions_root.rglob("*.png"))


def test_kopii_udalyayutsya_i_schitayutsya(tmp_path: Path) -> None:
    storage = filled(tmp_path)

    freed, removed = purge_marked(storage)

    assert removed == 6, "шесть копий: два урока по три ученика"
    assert freed == 6 * 500
    assert not list(storage.marked_root.rglob("*.png"))


def test_svezhiy_chernovik_ne_udalyaetsya(tmp_path: Path) -> None:
    """Автор может собирать урок прямо сейчас — вырывать файл из-под рук нельзя."""
    storage = Storage(root=tmp_path)
    staging = storage.new_staging_dir()
    Storage.staged_original(staging, 0).write_bytes(b"draft")

    freed, removed = purge_staging(storage, older_than=timedelta(days=1))

    assert (freed, removed) == (0, 0)
    assert staging.exists()


def test_broshennyy_chernovik_udalyaetsya(tmp_path: Path) -> None:
    storage = Storage(root=tmp_path)
    staging = storage.new_staging_dir()
    Storage.staged_original(staging, 0).write_bytes(b"draft")

    freed, removed = purge_staging(storage, older_than=timedelta())

    assert removed == 1 and freed == len(b"draft")
    assert not staging.exists()


def test_otchyot_vidit_kuda_ushlo_mesto(tmp_path: Path) -> None:
    """Ради этого всё и затевалось: по отчёту должно быть видно виновника."""
    storage = filled(tmp_path)

    current = usage(storage)

    assert current.by_folder["marked"] == (6 * 500, 6)
    assert current.by_folder["lessons"] == (2 * 1000, 2)
    assert current.total > 0


def test_razmery_chitayutsya_glazami() -> None:
    assert human(512) == "512 Б"
    assert human(2048) == "2.0 КБ"
    assert human(5 * 1024**3) == "5.0 ГБ"
