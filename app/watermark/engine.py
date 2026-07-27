"""Движок невидимого водяного знака: embed / extract / locate.

Алгоритм — blind-watermark 0.4.4 (DWT-DCT-SVD), подход один-в-один с проверенной
референс-реализацией. Отличия от референса — только в обвязке, не в математике:

* картинки читаются/пишутся через Pillow и передаются в библиотеку массивом.
  ``cv2.imread`` на Windows не открывает пути с кириллицей и молча возвращает
  ``None``, а временные файлы в cwd ломаются при параллельной разметке;
* PNG-конвейер остаётся без потерь, поэтому результат идентичен референсу;
* всё синхронное завёрнуто в ``asyncio.to_thread`` (см. ``*_async``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import cv2
import numpy as np
from blind_watermark import WaterMark, bw_notes
from numpy.typing import NDArray
from PIL import Image

from app.watermark.payload import WM_BIT_LENGTH, Payload

# Библиотека печатает рекламный баннер в stdout при первом создании WaterMark().
bw_notes.close()

logger = logging.getLogger(__name__)

#: Трёхканальное BGR-изображение — то, что ожидает blind-watermark.
BgrImage = NDArray[np.uint8]


def _u8(array: NDArray[Any]) -> NDArray[np.uint8]:
    """Стабы cv2 обещают размытый dtype, хотя на входе и выходе uint8."""
    return cast("NDArray[np.uint8]", array)


#: Версия процедуры встраивания. Меняется, когда меченые копии, сделанные
#: старым кодом, перестают быть эквивалентны новым, — тогда закэшированные
#: на диске копии надо перегенерировать, а не переиспользовать.
EMBED_VERSION: Final[int] = 3

#: Сила встраивания — шаг квантования старшего сингулярного числа блока.
#:
#: Умолчание библиотеки (36) на скриншоте терминала не выдерживает связку
#: «JPEG + уменьшение»: метка гибнет уже при 0.7 от исходного размера, хотя
#: чистое уменьшение держит до 0.3. На 48 всё восстанавливается до 0.4 —
#: проверено на четырёх разных графиках и двух метках, PSNR при этом
#: остаётся около 33 дБ (глазом отличий не видно).
#:
#: Выше не поднимаем: на 60 и 90 встраивание срывается совсем, причём с
#: обманчиво высоким PSNR. Это часть формата метки — менять значение задним
#: числом нельзя, уже выданные копии перестанут читаться.
EMBED_STRENGTH: Final[int] = 48

#: Потолок яркости, к которому приводится картинка ПЕРЕД встраиванием.
#:
#: На скриншоте светлого торгового терминала до 98% пикселей — чистый белый
#: (255). blind-watermark кодирует бит, СДВИГАЯ ВВЕРХ старшее сингулярное
#: число блока, и в сплошном белом сдвигать некуда: модуляция получается
#: вырожденной, JPEG возвращает блок к ровному тону, и метка гибнет при любом
#: сжатии — хотя из PNG ещё читается. Потолок возвращает ей запас.
#:
#: Только сверху: у нижней границы места хватает, тёмные картинки проходят
#: всю цепочку атак и без подготовки (см. тесты на тёмный график).
#:
#: Операция самонастраивающаяся: картинку без насыщенных пикселей она вообще
#: не трогает. Для белого фона это сдвиг на 4/255 = 1.6%, глазом не различимый.
EMBED_CEILING: Final[int] = 251


def add_headroom(image: BgrImage) -> BgrImage:
    """Оставить метке запас под верхней границей. См. ``EMBED_CEILING``."""
    return _u8(np.minimum(image, EMBED_CEILING))

# --- Параметры поиска графика на скриншоте -------------------------------------
# Масштаб задаётся целым процентом, а ширина шаблона считается как
# ``tpl_w * percent // 100`` — только целочисленно. Формула ``int(W * 0.6)``
# из референса даёт 575 вместо 576 (0.6 не представима в double), и промах
# даже в один пиксель растягивает кроп при нормализации, ломая извлечение.
#: Нижняя граница поиска намеренно ниже, чем читается метка: на скриншоте
#: всего экрана график бывает крошечной превьюшкой в ленте чата. Найти его
#: там всё равно полезно — тогда в ответе видно НАСТОЯЩУЮ причину отказа
#: («слишком мелко»), а не ложное совпадение с чужим уроком.
LOCATE_SCALE_MIN_PCT: Final[int] = 15

#: Примерно с какой доли исходного размера метка ещё восстанавливается.
#: Используется только для объяснения отказа, ничего не ограничивает.
READABLE_SCALE: Final[float] = 0.4
#: Референс перебирал до 1.05; верхняя граница поднята, чтобы ловить скриншоты
#: с HiDPI-экранов, где картинка отрисована крупнее оригинала.
LOCATE_SCALE_MAX_PCT: Final[int] = 200
#: Шаблон меньше этого размера бессмысленно искать.
LOCATE_MIN_SIDE: Final[int] = 50
#: Грубый проход идёт на уменьшенных копиях — так перебор масштабов дёшев.
LOCATE_COARSE_MAX_SIDE: Final[int] = 900
LOCATE_COARSE_STEP_PCT: Final[int] = 4
#: Полуширина окна точного прохода вокруг грубой оценки, в процентах масштаба.
LOCATE_FINE_WINDOW_PCT: Final[int] = 2 * LOCATE_COARSE_STEP_PCT
#: Финальная доводка по ширине, в пикселях: реальный скрин редко попадает
#: в круглый процент, а извлечение метки требует точной геометрии.
LOCATE_POLISH_PX: Final[int] = 3


class WatermarkError(RuntimeError):
    """Не удалось встроить метку."""


@dataclass(frozen=True, slots=True)
class EmbedResult:
    """Результат встраивания."""

    path: Path
    bit_length: int
    size: tuple[int, int]
    """Геометрия (width, height) — она же нужна при извлечении."""


@dataclass(frozen=True, slots=True)
class CoarseMatch:
    """Результат дешёвого прохода: насколько похоже и в каком масштабе."""

    confidence: float
    percent: int
    """Масштаб оригинала на скриншоте, в процентах."""


@dataclass(frozen=True, slots=True)
class LocateMatch:
    """Найденная на скриншоте область урока."""

    confidence: float
    """Максимум TM_CCOEFF_NORMED: 1.0 — идеальное совпадение."""

    box: tuple[int, int, int, int]
    """(x, y, width, height) в координатах присланной картинки."""

    crop: BgrImage


# --- Ввод-вывод ----------------------------------------------------------------


def load_bgr(path: Path) -> BgrImage:
    """Прочитать картинку и привести к BGR без альфа-канала.

    ``convert("RGB")`` снимает альфу, палитру и CMYK — ровно как в референсе.
    """
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return _u8(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def save_bgr(path: Path, image: BgrImage) -> None:
    """Сохранить BGR-массив. Для PNG — без потерь."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    Image.fromarray(rgb).save(path)


def _to_uint8(image: NDArray[np.generic]) -> BgrImage:
    """blind-watermark отдаёт float-массив; cv2.imwrite округляет — делаем то же."""
    if image.dtype == np.uint8:
        return image  # type: ignore[return-value]
    return np.round(np.clip(image, 0, 255)).astype(np.uint8)


def resize_to(image: BgrImage, size: tuple[int, int]) -> BgrImage:
    """Привести к геометрии оригинала (width, height) через LANCZOS, как в референсе."""
    width, height = size
    if (image.shape[1], image.shape[0]) == (width, height):
        return image
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    resized = Image.fromarray(rgb).resize((width, height), Image.Resampling.LANCZOS)
    return _u8(cv2.cvtColor(np.asarray(resized, dtype=np.uint8), cv2.COLOR_RGB2BGR))


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) без полной распаковки пикселей."""
    with Image.open(path) as image:
        return image.size


# --- Движок --------------------------------------------------------------------


class WatermarkEngine:
    """Встраивание и извлечение метки. Пароли приходят из окружения."""

    def __init__(self, password_img: int, password_wm: int) -> None:
        self._password_img = password_img
        self._password_wm = password_wm

    def _new(self) -> WaterMark:
        bwm = WaterMark(
            password_img=self._password_img,
            password_wm=self._password_wm,
        )
        # Сила задаётся и при встраивании, и при извлечении — значения обязаны
        # совпадать, иначе метка не прочитается.
        bwm.bwm_core.d1 = EMBED_STRENGTH
        return bwm

    def embed(self, src: Path, dst: Path, payload: Payload) -> EmbedResult:
        """Вшить метку в ``src`` и записать результат в ``dst`` (PNG, без потерь).

        Оригинал на диске остаётся нетронутым: запас по яркости получает только
        выдаваемая копия. Для выравнивания при трассировке нужна геометрия
        оригинала, а не его пиксели, поэтому пристинность не страдает.
        """
        source = add_headroom(load_bgr(src))
        height, width = source.shape[:2]

        bwm = self._new()
        bwm.read_img(img=source)
        bwm.read_wm(payload.encode(), mode="str")

        try:
            marked = _to_uint8(bwm.embed())
        except AssertionError as exc:  # картинка мельче, чем нужно под метку
            raise WatermarkError(
                f"картинка {width}x{height} слишком мала для метки: {exc}"
            ) from exc

        if marked.shape[:2] != (height, width):  # pragma: no cover - страховка
            raise WatermarkError(
                f"размер после встраивания изменился: {marked.shape[:2]} != {(height, width)}"
            )

        save_bgr(dst, marked)
        bit_length: int = int(np.size(bwm.wm_bit))
        if bit_length != WM_BIT_LENGTH:  # pragma: no cover - страховка на формат
            raise WatermarkError(
                f"неожиданная длина метки {bit_length}, ожидалось {WM_BIT_LENGTH}"
            )
        return EmbedResult(path=dst, bit_length=bit_length, size=(width, height))

    def extract(
        self,
        image: Path | BgrImage,
        original_size: tuple[int, int],
        *,
        bit_length: int = WM_BIT_LENGTH,
    ) -> Payload | None:
        """Достать метку из подозрительной картинки.

        ``original_size`` — геометрия пристинного оригинала урока: перед
        извлечением картинку обязательно возвращают к ней, иначе блоки DWT
        не совпадут. ``None`` — метка не читается (пережато, обрезано, не тот урок).
        """
        raw = self.extract_raw(image, original_size, bit_length=bit_length)
        if raw is None:
            return None
        return Payload.parse(raw)

    def extract_raw(
        self,
        image: Path | BgrImage,
        original_size: tuple[int, int],
        *,
        bit_length: int = WM_BIT_LENGTH,
    ) -> str | None:
        """То же, но без разбора формата — полезно для диагностики."""
        source = load_bgr(image) if isinstance(image, Path) else image
        normalized = resize_to(source, original_size)

        bwm = self._new()
        try:
            return str(
                bwm.extract(embed_img=normalized, wm_shape=bit_length, mode="str")
            )
        except (ValueError, AssertionError, IndexError) as exc:
            # ValueError прилетает из bytes.fromhex, когда биты собрались в мусор;
            # AssertionError — когда картинка мельче минимального блока.
            logger.debug("извлечение не удалось: %s", exc)
            return None


# --- Поиск графика на скриншоте ------------------------------------------------


def coarse_percents() -> list[int]:
    """Сетка масштабов грубого прохода. Строится ОТ 100% в обе стороны.

    100% — самый частый случай: утёкшую картинку чаще всего просто пересылают
    как есть. При этом промах даже на 2% по масштабу роняет корреляцию на
    детализированном графике в разы (0.99 против 0.43), и урок не опознаётся.
    Наивная сетка ``range(30, 201, 4)`` даёт 30, 34, ... 198 — ровно 100 в неё
    и не попадает, поэтому отсчёт идёт от 100, а не от нижней границы.
    """
    step = LOCATE_COARSE_STEP_PCT
    down = range(100, LOCATE_SCALE_MIN_PCT - 1, -step)
    up = range(100, LOCATE_SCALE_MAX_PCT + 1, step)
    return sorted({*down, *up})


def _percent_widths(template_width: int, percents: Iterable[int]) -> list[int]:
    """Ширины шаблона для заданных масштабов — целочисленно.

    Именно целочисленно: ``int(W * 0.6)`` даёт 575 вместо 576, потому что 0.6
    не представима в double, а промах в пиксель ломает извлечение метки.
    """
    return sorted(
        {
            template_width * pct // 100
            for pct in percents
            if LOCATE_SCALE_MIN_PCT <= pct <= LOCATE_SCALE_MAX_PCT
        }
    )


def _match_best(
    shot: NDArray[np.uint8],
    template: NDArray[np.uint8],
    widths: list[int],
) -> tuple[float, int, int, int, int] | None:
    """Перебор ширин: вернуть (confidence, x, y, w, h) лучшего совпадения."""
    tpl_h, tpl_w = template.shape[:2]
    shot_h, shot_w = shot.shape[:2]
    best: tuple[float, int, int, int, int] | None = None

    for width in widths:
        height = round(tpl_h * width / tpl_w)
        if width < LOCATE_MIN_SIDE or height < LOCATE_MIN_SIDE:
            continue
        if width > shot_w or height > shot_h:
            continue
        interpolation = cv2.INTER_AREA if width <= tpl_w else cv2.INTER_CUBIC
        resized = _u8(cv2.resize(template, (width, height), interpolation=interpolation))
        response = cv2.matchTemplate(shot, resized, cv2.TM_CCOEFF_NORMED)
        _, max_value, _, max_location = cv2.minMaxLoc(response)
        if best is None or max_value > best[0]:
            best = (float(max_value), int(max_location[0]), int(max_location[1]), width, height)

    return best


def _gray(image: Path | BgrImage) -> NDArray[np.uint8]:
    source = load_bgr(image) if isinstance(image, Path) else image
    return _u8(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY))


def locate_coarse(screenshot: Path | BgrImage, original: Path | BgrImage) -> CoarseMatch | None:
    """Дешёвая прикидка «похоже / не похоже» на уменьшенных копиях.

    При трассировке уроков может быть много, а точный проход стоит секунды.
    Сначала этим отбирают кандидатов, и только победители идут в :func:`locate`.
    """
    shot_gray = _gray(screenshot)
    tpl_gray = _gray(original)

    shot_h, shot_w = shot_gray.shape[:2]
    tpl_h, tpl_w = tpl_gray.shape[:2]
    if min(shot_h, shot_w, tpl_h, tpl_w) < LOCATE_MIN_SIDE:
        return None

    # Общий коэффициент уменьшения для скрина и шаблона — геометрия не плывёт.
    shrink = min(1.0, LOCATE_COARSE_MAX_SIDE / max(shot_h, shot_w))
    if shrink < 1.0:
        small_shot = _u8(
            cv2.resize(
                shot_gray,
                (round(shot_w * shrink), round(shot_h * shrink)),
                interpolation=cv2.INTER_AREA,
            )
        )
        small_tpl = _u8(
            cv2.resize(
                tpl_gray,
                (round(tpl_w * shrink), round(tpl_h * shrink)),
                interpolation=cv2.INTER_AREA,
            )
        )
    else:
        small_shot, small_tpl = shot_gray, tpl_gray

    coarse = _match_best(
        small_shot, small_tpl, _percent_widths(small_tpl.shape[1], coarse_percents())
    )
    if coarse is None:
        return None
    return CoarseMatch(
        confidence=coarse[0],
        percent=round(100 * coarse[3] / max(small_tpl.shape[1], 1)),
    )


def locate(
    screenshot: Path | BgrImage,
    original: Path | BgrImage,
    *,
    hint: CoarseMatch | None = None,
) -> LocateMatch | None:
    """Найти область урока на полноэкранном скриншоте (multi-scale template match).

    Три прохода: грубый на уменьшенных копиях (дёшево нащупать масштаб), точный
    на полном разрешении в окрестности найденного и доводка по ширине ±3 px.
    Кроп всегда берётся из исходного разрешения — иначе пострадает извлечение.

    ``hint`` — уже посчитанный результат :func:`locate_coarse`, тогда первый
    проход пропускается.
    """
    shot_bgr = load_bgr(screenshot) if isinstance(screenshot, Path) else screenshot
    tpl_bgr = load_bgr(original) if isinstance(original, Path) else original

    coarse = hint if hint is not None else locate_coarse(shot_bgr, tpl_bgr)
    if coarse is None:
        return None

    shot_gray = _u8(cv2.cvtColor(shot_bgr, cv2.COLOR_BGR2GRAY))
    tpl_gray = _u8(cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY))
    tpl_w = tpl_gray.shape[1]

    # Проход 2 — точный, полное разрешение, окно вокруг грубой оценки.
    best = _match_best(
        shot_gray,
        tpl_gray,
        _percent_widths(
            tpl_w,
            range(
                coarse.percent - LOCATE_FINE_WINDOW_PCT,
                coarse.percent + LOCATE_FINE_WINDOW_PCT + 1,
            ),
        ),
    )
    if best is None:
        return None

    # Проход 3 — доводка ±3 px: реальный скрин почти никогда не попадает в круглый процент.
    polished = _match_best(
        shot_gray,
        tpl_gray,
        [best[3] + delta for delta in range(-LOCATE_POLISH_PX, LOCATE_POLISH_PX + 1)],
    )
    if polished is not None and polished[0] > best[0]:
        best = polished

    confidence, x, y, width, height = best
    crop = shot_bgr[y : y + height, x : x + width].copy()
    if crop.size == 0:  # pragma: no cover - страховка
        return None
    return LocateMatch(confidence=confidence, box=(x, y, width, height), crop=crop)


# --- Async-обёртки -------------------------------------------------------------


async def embed_async(
    engine: WatermarkEngine, src: Path, dst: Path, payload: Payload
) -> EmbedResult:
    return await asyncio.to_thread(engine.embed, src, dst, payload)


async def extract_async(
    engine: WatermarkEngine,
    image: Path | BgrImage,
    original_size: tuple[int, int],
) -> Payload | None:
    return await asyncio.to_thread(engine.extract, image, original_size)


async def locate_async(
    screenshot: Path | BgrImage,
    original: Path | BgrImage,
    *,
    hint: CoarseMatch | None = None,
) -> LocateMatch | None:
    return await asyncio.to_thread(locate, screenshot, original, hint=hint)


async def locate_coarse_async(
    screenshot: Path | BgrImage, original: Path | BgrImage
) -> CoarseMatch | None:
    return await asyncio.to_thread(locate_coarse, screenshot, original)
