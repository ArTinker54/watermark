"""Движок водяного знака: embed -> атака -> extract.

Границы устойчивости, которые здесь зафиксированы, соответствуют проверенным
на реальном материале клиента: держит jpeg>=50, даунскейл до ~0.5, двойное
сжатие плюс скриншот. Ломается на jpeg30 — это осознанная граница MVP.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from blind_watermark import WaterMark
from PIL import Image

from app.watermark import Payload, WatermarkEngine, locate
from app.watermark.engine import (
    LOCATE_FINE_WINDOW_PCT,
    add_headroom,
    coarse_percents,
    locate_coarse,
)
from app.watermark.payload import WM_BIT_LENGTH
from tests.conftest import (
    PW_IMG,
    PW_WM,
    chat_window,
    desktop_screenshot,
    flat_share,
    jpeg,
    make_terminal_chart,
    psnr,
    rescale,
    screenshot,
    to_bgr,
)

PAYLOAD = Payload(uid=42, lesson_id=1)


@pytest.fixture(scope="session")
def marked(engine: WatermarkEngine, chart_path: Path, tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("marked") / "lesson_0042.png"
    result = engine.embed(chart_path, out, PAYLOAD)
    return result


def test_wm_bit_length_matches_library() -> None:
    """WM_BIT_LENGTH посчитан формулой — сверяем с тем, что реально делает библиотека."""
    bwm = WaterMark(password_img=PW_IMG, password_wm=PW_WM)
    bwm.read_wm(PAYLOAD.encode(), mode="str")
    assert int(bwm.wm_bit.size) == WM_BIT_LENGTH


def test_embed_keeps_geometry(marked, chart_path: Path) -> None:
    """Меченая копия обязана совпадать по размеру с оригиналом."""
    with Image.open(chart_path) as original, Image.open(marked.path) as result:
        assert original.size == result.size == marked.size


def test_embed_is_visually_identical(marked, chart_path: Path) -> None:
    with Image.open(chart_path) as original, Image.open(marked.path) as result:
        assert psnr(original, result) > 35.0


def test_extract_from_clean_copy(engine: WatermarkEngine, marked) -> None:
    assert engine.extract(marked.path, marked.size) == PAYLOAD


def test_wrong_password_does_not_read(marked) -> None:
    """Чужой пароль не должен давать валидную метку — иначе метка ничего не доказывает."""
    stranger = WatermarkEngine(password_img=PW_IMG + 1, password_wm=PW_WM + 1)
    assert stranger.extract(marked.path, marked.size) is None


@pytest.mark.parametrize("quality", [90, 75, 50])
def test_survives_jpeg(engine: WatermarkEngine, marked, quality: int) -> None:
    with Image.open(marked.path) as image:
        attacked = jpeg(image, quality)
    assert engine.extract(to_bgr(attacked), marked.size) == PAYLOAD


@pytest.mark.parametrize("factor", [0.75, 0.5])
def test_survives_downscale(engine: WatermarkEngine, marked, factor: float) -> None:
    with Image.open(marked.path) as image:
        attacked = rescale(image, factor)
    assert engine.extract(to_bgr(attacked), marked.size) == PAYLOAD


def test_survives_full_compression_chain(engine: WatermarkEngine, marked) -> None:
    """Ключевой сценарий: jpeg80 -> jpeg75 -> скриншот (0.5) -> jpeg90."""
    with Image.open(marked.path) as image:
        attacked = jpeg(image, 80)
    attacked = jpeg(attacked, 75)
    attacked = rescale(attacked, 0.5)
    attacked = jpeg(attacked, 90)

    assert engine.extract(to_bgr(attacked), marked.size) == PAYLOAD


def test_trace_from_full_screen_screenshot(
    engine: WatermarkEngine, marked, chart_path: Path
) -> None:
    """Полный путь трассировки: скрин экрана -> locate по оригиналу -> extract."""
    with Image.open(marked.path) as image:
        shot = screenshot(jpeg(image, 85), scale=0.6, offset=(140, 220))
    shot = jpeg(shot, 88)

    match = locate(to_bgr(shot), chart_path)
    assert match is not None
    assert match.confidence > 0.5, f"график не найден на скрине: {match.confidence:.3f}"

    assert engine.extract(match.crop, marked.size) == PAYLOAD


def test_locate_picks_the_right_lesson(
    engine: WatermarkEngine, marked, chart_path: Path, tmp_path: Path
) -> None:
    """При переборе уроков побеждать должен тот оригинал, что реально на скрине."""
    from tests.conftest import make_chart

    other = tmp_path / "other_lesson.png"
    make_chart(seed=99).save(other)

    with Image.open(marked.path) as image:
        shot = to_bgr(jpeg(screenshot(image, scale=0.55, offset=(200, 180)), 85))

    right = locate(shot, chart_path)
    wrong = locate(shot, other)
    assert right is not None and wrong is not None
    assert right.confidence > wrong.confidence


def test_jpeg30_is_out_of_scope(engine: WatermarkEngine, marked) -> None:
    """Зафиксированная граница: на jpeg30 метка не выживает и мы это знаем."""
    with Image.open(marked.path) as image:
        attacked = jpeg(image, 30)
    assert engine.extract(to_bgr(attacked), marked.size) != PAYLOAD


# --- Однотонный фон: реальный материал клиента --------------------------------


def test_terminal_chart_is_mostly_flat() -> None:
    """Проверка предпосылки: скрин терминала почти целиком однотонный."""
    assert flat_share(make_terminal_chart()) > 0.9
    assert flat_share(make_terminal_chart(dark=True)) > 0.9


def test_headroom_does_not_touch_unsaturated_images() -> None:
    """На картинке без насыщенных пикселей подготовка — пустая операция."""
    image = np.full((16, 16, 3), 128, dtype=np.uint8)
    assert np.array_equal(add_headroom(image), image)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("quality", [95, 87, 80])
def test_survives_jpeg_on_flat_chart(
    engine: WatermarkEngine, tmp_path: Path, dark: bool, quality: int
) -> None:
    """Скрин терминала: без запаса по яркости метка гибнет уже на jpeg95.

    Из PNG она при этом читается, поэтому проверять надо именно сжатие —
    ровно так дефект и дожил до боевой рассылки.
    """
    source = tmp_path / f"terminal_{dark}.png"
    make_terminal_chart(dark=dark).save(source)
    result = engine.embed(source, tmp_path / f"marked_{dark}.png", PAYLOAD)

    with Image.open(result.path) as image:
        attacked = jpeg(image, quality)
    assert engine.extract(to_bgr(attacked), result.size) == PAYLOAD


def test_flat_chart_survives_screenshot_chain(engine: WatermarkEngine, tmp_path: Path) -> None:
    source = tmp_path / "terminal.png"
    make_terminal_chart().save(source)
    result = engine.embed(source, tmp_path / "marked.png", PAYLOAD)

    with Image.open(result.path) as image:
        attacked = jpeg(rescale(jpeg(jpeg(image, 87), 80), 0.5), 90)
    assert engine.extract(to_bgr(attacked), result.size) == PAYLOAD


# --- Поиск урока --------------------------------------------------------------


def test_coarse_grid_contains_identity_scale() -> None:
    """Утёкшую картинку чаще всего пересылают как есть — масштаб ровно 100%."""
    assert 100 in coarse_percents()


@pytest.mark.parametrize("scale", [0.8, 0.6, 0.5, 0.4])
def test_flat_chart_survives_jpeg_plus_downscale(
    engine: WatermarkEngine, tmp_path: Path, scale: float
) -> None:
    """Связка «сжатие + уменьшение» — то, что реально делает скриншот.

    Именно она, а не сжатие само по себе, убивала метку на однотонном графике
    при силе встраивания по умолчанию.
    """
    source = tmp_path / "terminal.png"
    make_terminal_chart().save(source)
    result = engine.embed(source, tmp_path / "marked.png", PAYLOAD)

    with Image.open(result.path) as image:
        attacked = jpeg(rescale(jpeg(image, 87), scale), 90)
    assert engine.extract(to_bgr(attacked), result.size) == PAYLOAD


def test_locate_finds_small_preview_on_desktop_screenshot(
    engine: WatermarkEngine, tmp_path: Path
) -> None:
    """Мелкую превьюшку надо НАЙТИ, даже если метку из неё уже не достать.

    Иначе система показывает ложное совпадение с чужим уроком вместо честного
    «слишком мелко» — а это подрывает доверие к результату трассировки.
    """
    source = tmp_path / "terminal.png"
    make_terminal_chart().save(source)
    result = engine.embed(source, tmp_path / "marked.png", PAYLOAD)

    offset = (360, 120)
    with Image.open(result.path) as image:
        shot = to_bgr(jpeg(desktop_screenshot(image, 0.2, offset=offset), 90))

    match = locate(shot, source)
    assert match is not None
    # До расширения диапазона поиска здесь было ~0.09 — уровень чистого шума.
    assert match.confidence > 0.6, f"превью не найдено: {match.confidence:.0%}"
    assert match.box[:2] == offset
    # Точность до пикселя тут недостижима: превью уменьшено LANCZOS, а шаблон
    # при переборе — INTER_AREA, и оптимум корреляции сдвигается на пиксель.
    assert abs(match.box[2] - int(result.size[0] * 0.2)) <= 3


def test_locate_finds_forwarded_copy(engine: WatermarkEngine, tmp_path: Path) -> None:
    """Копия 1-в-1 обязана опознаваться уверенно, а не на уровне шума."""
    source = tmp_path / "terminal.png"
    make_terminal_chart().save(source)
    result = engine.embed(source, tmp_path / "marked.png", PAYLOAD)

    with Image.open(result.path) as image:
        forwarded = to_bgr(jpeg(image, 87))

    match = locate(forwarded, source)
    assert match is not None
    assert match.confidence > 0.9, f"совпадение всего {match.confidence:.0%}"
    assert match.box[2:] == result.size
    assert engine.extract(match.crop, result.size) == PAYLOAD



#: Окна мессенджера, снятые с реальных экранов. Размер тут не декорация:
#: грубый проход ужимает скриншот под 900 px, и от размера окна зависит, в
#: какую точку сетки перебора попадёт настоящий масштаб.
WINDOWS = [(1053, 900), (1280, 800), (1440, 900), (1920, 1080)]


@pytest.fixture(scope="session")
def lesson_800(tmp_path_factory: pytest.TempPathFactory):
    """Боевая геометрия урока: тёмный график, длинная сторона 800 (LESSON_MAX_SIDE).

    Session-scope намеренно: встраивание метки — самая дорогая операция в этом
    файле, а перебор окон и масштабов сам по себе дешёвый.
    """
    engine = WatermarkEngine(password_img=PW_IMG, password_wm=PW_WM)
    folder = tmp_path_factory.mktemp("lesson800")
    source = folder / "terminal.png"
    make_terminal_chart(width=800, height=518, dark=True).save(source)
    result = engine.embed(source, folder / "marked.png", PAYLOAD)
    with Image.open(result.path) as image:
        # Ученику картинка приходит фотографией, то есть уже пережатой Telegram.
        received = jpeg(image, 87)
    return source, result, received


@pytest.mark.parametrize("canvas", WINDOWS)
@pytest.mark.parametrize("scale", [0.45, 0.5, 0.55, 0.65, 0.7, 0.8, 0.9, 0.95])
def test_coarse_pass_lands_in_fine_window(
    lesson_800, canvas: tuple[int, int], scale: float
) -> None:
    """Договор между двумя проходами поиска, который раньше нарушался.

    Точный проход просматривает только окно ±``LOCATE_FINE_WINDOW_PCT`` вокруг
    оценки грубого. Значит промах грубого прохода дальше этого окна — не
    неточность, а потеря: настоящий масштаб не будет просмотрен никогда, и
    метку прочитать станет неоткуда.

    Проверяется именно инвариант, а не итог целиком: он дешёвый, поэтому его
    можно прогнать по сетке окон и масштабов, а не по трём удобным. Раньше на
    этой сетке промахивалась примерно треть сочетаний.
    """
    source, _result, received = lesson_800
    shot = to_bgr(jpeg(chat_window(received, scale, canvas=canvas), 90))

    coarse = locate_coarse(shot, source)
    assert coarse is not None, "урок не опознан вовсе"

    shown = round(scale * 100)
    assert abs(coarse.percent - shown) <= LOCATE_FINE_WINDOW_PCT, (
        f"окно {canvas[0]}×{canvas[1]}: грубый проход сказал {coarse.percent}% "
        f"вместо {shown}% — точный будет искать в "
        f"{coarse.percent - LOCATE_FINE_WINDOW_PCT}.."
        f"{coarse.percent + LOCATE_FINE_WINDOW_PCT}% и настоящий масштаб пропустит"
    )


@pytest.mark.parametrize("scale", [0.5, 0.65, 0.7, 0.9, 0.95])
def test_locate_survives_any_display_scale(
    engine: WatermarkEngine, lesson_800, scale: float
) -> None:
    """Тот же дефект, но целиком: от скриншота до прочитанной метки.

    Пришло из жизни: трассировка отвечала «урок найден, но метка не читается»
    практически на всё. Урок опознавался, а область на скриншоте выделялась
    крошечная — 126×82 вместо 720×466, — и читать метку было уже неоткуда.

    Старые тесты брали масштабы 0.6, 0.55 и 0.2 в окне 1600×1000 и случайно
    попадали в удачные сочетания, поэтому дефект прожил до боевой эксплуатации.
    """
    source, result, received = lesson_800
    shot = to_bgr(jpeg(chat_window(received, scale), 90))

    match = locate(shot, source)
    assert match is not None, "область урока не найдена вовсе"

    expected = round(result.size[0] * scale)
    assert abs(match.box[2] - expected) <= 3, (
        f"выделена не та область: {match.box[2]}×{match.box[3]} вместо ~{expected} "
        f"(похожесть {match.confidence:.2f})"
    )
    assert engine.extract(match.crop, result.size) == PAYLOAD
