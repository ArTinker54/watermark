"""Разбор .env: пустые значения и списки id."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

_REQUIRED = {
    "admin_bot_token": "a:1",
    "student_bot_token": "b:2",
    "wm_pw_img": 1,
    "wm_pw_wm": 2,
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_optional_means_disabled(raw: str) -> None:
    """``VSA_GROUP_ID=`` — это выключенная проверка, а не ошибка конфига."""
    assert _settings(vsa_group_id=raw).vsa_group_id is None


@pytest.mark.parametrize("field", ["db_path", "storage_path", "offer_path"])
def test_blank_path_falls_back_to_default(field: str) -> None:
    default = getattr(_settings(), field)
    assert getattr(_settings(**{field: ""}), field) == default


def test_group_id_is_parsed_when_set() -> None:
    assert _settings(vsa_group_id="-1001234567890").vsa_group_id == -1001234567890


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", set()),
        ("123", {123}),
        ("123,456", {123, 456}),
        ("123, 456 ; 789", {123, 456, 789}),
    ],
)
def test_admin_ids_parsing(raw: str, expected: set[int]) -> None:
    assert set(_settings(admin_ids=raw).admin_id_set) == expected


def test_relative_paths_are_resolved_from_project_root() -> None:
    """Иначе путь бы зависел от того, из какой папки запустили процесс."""
    assert _settings(db_path="data/db.sqlite3").db_path.is_absolute()


def test_watermark_password_range_is_enforced() -> None:
    """Пароль вне диапазона seed для numpy уронил бы первую же рассылку."""
    with pytest.raises(ValueError):
        _settings(wm_pw_img=2**32)


def test_offer_file_from_repo_is_readable() -> None:
    from app.texts import load_offer

    text = load_offer(Path(_settings().offer_path))
    assert "автор" in text.lower(), "оферта должна закреплять авторские права"
    assert "персональные данные" in text.lower(), "состав хранимых данных обязан быть раскрыт"


def test_offer_says_nothing_false_about_marking() -> None:
    """Про метку в оферте не сказано — это решение заказчика.

    Умолчание допустимо, прямая ложь нет. Тест ловит появление утверждений
    вроде «материалы не содержат меток»: они были бы неправдой, потому что
    метка вшивается в каждую выданную копию.
    """
    from app.texts import load_offer

    text = load_offer(Path(_settings().offer_path)).lower()
    for claim in ("не содержат мет", "без мет", "не помечен", "не отслеж"):
        assert claim not in text, f"в оферте появилось ложное утверждение: {claim!r}"
