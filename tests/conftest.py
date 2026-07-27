"""Общие фикстуры и утилиты атак на картинку."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.watermark import WatermarkEngine

PW_IMG = 20240517
PW_WM = 76543


@pytest.fixture(scope="session")
def engine() -> WatermarkEngine:
    return WatermarkEngine(password_img=PW_IMG, password_wm=PW_WM)


def make_chart(width: int = 960, height: int = 600, seed: int = 7) -> Image.Image:
    """Синтетический график, похожий на обучающий материал.

    Сетка, оси, ломаная и подписи дают текстуру, сопоставимую с реальным
    графиком клиента, — на плоской заливке ни один частотный знак не живёт.
    """
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(image)

    margin = 60
    plot_w = width - 2 * margin
    plot_h = height - 2 * margin

    for i in range(11):
        x = margin + plot_w * i // 10
        y = margin + plot_h * i // 10
        draw.line([(x, margin), (x, height - margin)], fill=(224, 224, 224), width=1)
        draw.line([(margin, y), (width - margin, y)], fill=(224, 224, 224), width=1)

    draw.rectangle([margin, margin, width - margin, height - margin], outline=(90, 90, 90), width=2)

    points = 64
    series = np.cumsum(rng.normal(0, 1.0, points))
    series -= series.min()
    if series.max() > 0:
        series /= series.max()
    xs = np.linspace(margin, width - margin, points)
    ys = height - margin - series * plot_h
    draw.line(
        [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)],
        fill=(0, 110, 200),
        width=3,
    )

    bars = 16
    values = rng.uniform(0.15, 0.85, bars)
    bar_w = plot_w / (bars * 1.6)
    for i, value in enumerate(values):
        x0 = margin + plot_w * (i + 0.3) / bars
        y0 = height - margin - value * plot_h * 0.5
        draw.rectangle([x0, y0, x0 + bar_w, height - margin - 1], fill=(230, 140, 60))

    draw.text((margin, 24), "VSA PRO - Lesson chart", fill=(30, 30, 30))
    draw.text((margin, height - 40), "volume / spread / close", fill=(60, 60, 60))
    return image


@pytest.fixture(scope="session")
def chart_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("originals") / "lesson.png"
    make_chart().save(path)
    return path


# --- Атаки ---------------------------------------------------------------------


def jpeg(image: Image.Image, quality: int) -> Image.Image:
    """Прогнать через JPEG заданного качества."""
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def rescale(image: Image.Image, factor: float) -> Image.Image:
    """Даунскейл/апскейл — имитация скриншота с другого DPI."""
    size = (max(1, int(image.width * factor)), max(1, int(image.height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS)


def screenshot(
    image: Image.Image,
    *,
    canvas: tuple[int, int] = (1600, 1000),
    scale: float = 0.6,
    offset: tuple[int, int] = (140, 220),
) -> Image.Image:
    """Вклеить картинку в «экран» с интерфейсом вокруг."""
    shot = Image.new("RGB", canvas, (23, 26, 31))
    draw = ImageDraw.Draw(shot)
    draw.rectangle([0, 0, canvas[0], 90], fill=(37, 41, 48))
    draw.text((32, 36), "Telegram - VSA PRO", fill=(220, 220, 220))
    draw.rectangle([0, canvas[1] - 70, canvas[0], canvas[1]], fill=(37, 41, 48))
    shot.paste(rescale(image, scale), offset)
    return shot


def to_bgr(image: Image.Image) -> np.ndarray:
    """PIL RGB -> BGR-массив для движка."""
    return np.asarray(image.convert("RGB"), dtype=np.uint8)[:, :, ::-1].copy()


def psnr(a: Image.Image, b: Image.Image) -> float:
    """Пиковое отношение сигнал/шум, дБ. >40 — глазом отличий не видно."""
    left = np.asarray(a.convert("RGB"), dtype=np.float64)
    right = np.asarray(b.convert("RGB"), dtype=np.float64)
    mse = float(np.mean((left - right) ** 2))
    if mse == 0:
        return float("inf")
    return 10.0 * float(np.log10(255.0**2 / mse))
