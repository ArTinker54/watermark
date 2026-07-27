"""Конфигурация приложения. Все секреты берутся только из окружения / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Настройки, общие для обоих ботов."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    admin_bot_token: str = Field(description="Токен закрытого бота автора")
    student_bot_token: str = Field(description="Токен публичного бота учеников")
    admin_ids: str = Field(
        default="",
        description="Telegram user_id администраторов через запятую",
    )
    vsa_group_id: int | None = Field(
        default=None,
        description="ID группы VSA PRO для проверки членства (опционально)",
    )

    # --- Водяной знак ---
    # Оба пароля уходят в numpy.random.RandomState как seed, а он принимает
    # только 0..2**32-1. Значение вне диапазона уронило бы бота на первой же
    # рассылке, поэтому проверяем на старте.
    wm_pw_img: int = Field(
        ge=0, le=2**32 - 1, description="Пароль встраивания в изображение"
    )
    wm_pw_wm: int = Field(ge=0, le=2**32 - 1, description="Пароль самой метки")

    # --- Хранилище ---
    db_path: Path = Field(default=Path("data/db.sqlite3"))
    storage_path: Path = Field(default=Path("data/storage"))
    offer_path: Path = Field(default=Path("app/texts/offer.txt"))

    # --- Поведение ---
    broadcast_rate: float = Field(
        default=20.0,
        gt=0,
        description="Максимум сообщений в секунду при рассылке (лимит Telegram ~30)",
    )
    wm_workers: int = Field(
        default=4,
        ge=1,
        description="Сколько картинок метить параллельно",
    )
    trace_min_confidence: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Порог совпадения template match, ниже которого урок не рассматривается",
    )
    log_level: str = Field(default="INFO")

    @field_validator(
        "vsa_group_id", "db_path", "storage_path", "offer_path", mode="before"
    )
    @classmethod
    def _blank_means_default(cls, value: Any, info: ValidationInfo) -> Any:
        """Пустая строка в .env — это «не задано», а не значение.

        Без этого закомментировать переменную нельзя: строка ``VSA_GROUP_ID=``
        роняет запуск на разборе int, хотя ровно так выключение и выглядит.
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[str(info.field_name)].default
        return value

    @field_validator("db_path", "storage_path", "offer_path", mode="after")
    @classmethod
    def _absolutize(cls, value: Path) -> Path:
        """Относительные пути считаем от корня проекта, а не от cwd."""
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def admin_id_set(self) -> frozenset[int]:
        raw = self.admin_ids.replace(";", ",").replace(" ", ",")
        return frozenset(int(part) for part in raw.split(",") if part.strip())

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def send_interval(self) -> float:
        """Минимальная пауза между отправками, секунды."""
        return 1.0 / self.broadcast_rate


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек (кэш, чтобы .env читался один раз за процесс)."""
    return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
