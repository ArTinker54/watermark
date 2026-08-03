"""Сверка прочитанной метки со списком реально выданных копий.

Формат ``V|uid|lesson`` не содержит избыточности: 95 бит, и почти любая
одиночная ошибка даёт другую формально валидную строку. Перебор всех 95
одиночных сбоев метки ``V|0042|00001`` показывает: 34 проходят разбор формата,
а 14 меняют ИМЕННО uid, оставляя номер урока нетронутым. Сверка урока их не
ловит по определению, и дальше находится реальный ученик, у которого в журнале
записана ровно эта строка, — то есть система уверенно называет невиновного.

Избыточность берётся не из формата, а из журнала: по каждому уроку известно,
какие именно метки были выданы. Поэтому здесь биты не округляются в строку, а
сравниваются со всеми выданными копиями. Побеждает ближайшая — и только если
она оторвалась от второго места. Стоимость одиночного сбоя тогда ровно равна
уверенности в сбитом бите: пока он читается уверенно, вердикт не меняется, а
когда бит стоит у самой границы, ответом становится честное «метка повреждена».
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

#: Насколько победитель обязан оторваться от второго места. Значение подобрано
#: измерением, а не на глаз: боевой оригинал урока прогонялся через 390 сочетаний
#: сжатия (JPEG 95..30) и уменьшения (100%..40%).
#:
#: Точное совпадение с выданной копией само по себе снимает почти всю опасность,
#: но не всю: из 131 «уверенного» попадания 4 назвали ЧУЖОГО человека — сбой
#: ровно одного бита превращал метку в соседскую. Отрыв в этих четырёх случаях
#: был 0.093, 0.074, 0.049 и 0.009, тогда как у верных ответов медиана 0.41.
#: Порог 0.20 отсекает все четыре с запасом вдвое и сохраняет 90% верных.
#:
#: Цена ошибки несимметрична: отказ означает «пришлите скриншот получше», а
#: ложное совпадение — обвинение невиновного. Поэтому порог выбран с запасом.
MIN_MARGIN: Final[float] = 0.20

#: Уверенность самого шаткого бита показывается в диагностике, но в решении не
#: участвует намеренно. Шаткий бит опасен, только если по нему расходятся две
#: ВЫДАННЫЕ метки, — а это уже измерено отрывом. Требовать сверх того значит
#: отбрасывать безупречные чтения: на боевых данных встречается отрыв 1.17 при
#: самом шатком бите 0.06.

#: До скольких разошедшихся бит прочитанное ещё считается повреждённой меткой,
#: а не её отсутствием. Чистый шум расходится примерно с половиной бит (для 95
#: бит это 47±5), поэтому 8 — это восемь сигм от случайного совпадения. Граница
#: нужна не ради математики: без неё «метку не видно, картинка слишком мелкая»
#: подменяется бесполезным «метка повреждена» на любой посторонней картинке.
MAX_DAMAGED_BITS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class SoftRead:
    """Мягкое чтение метки — значение каждого бита до округления.

    ``threshold`` — граница между нулями и единицами, посчитанная тем же
    способом, что и в библиотеке. Уверенность в бите — это расстояние от него
    до границы, отнесённое к общему разбросу значений.
    """

    values: tuple[float, ...]
    threshold: float

    @property
    def spread(self) -> float:
        """Масштаб, в котором меряется уверенность: полуразмах значений."""
        if not self.values:
            return 1.0
        half = (max(self.values) - min(self.values)) / 2
        return half if half > 1e-9 else 1.0

    @property
    def weakest_bit(self) -> float:
        """Уверенность самого шаткого бита, от 0 до 1."""
        if not self.values:
            return 0.0
        return min(abs(value - self.threshold) for value in self.values) / self.spread


@dataclass(frozen=True, slots=True)
class Verdict:
    """Итог сверки: какая из выданных копий объясняет прочитанное лучше всех."""

    payload: str
    mismatched: int
    """Сколько бит разошлось с этой копией. 0 — прочитано ровно её."""

    margin: float
    """Отрыв от второго места в долях уверенности одного бита."""

    runner_up: str | None
    weakest_bit: float

    @property
    def trustworthy(self) -> bool:
        """Можно ли называть человека.

        Два условия: прочитано ровно то, что выдавали, и следующий кандидат
        заметно хуже. Второе и есть защита от одиночного сбоя — отрыв равен
        удвоенной уверенности того бита, по которому расходятся победитель и
        ближайшая чужая метка.
        """
        return self.mismatched == 0 and self.margin >= MIN_MARGIN

    @property
    def plausible(self) -> bool:
        """Похоже ли прочитанное на метку вообще.

        Отличает «метка есть, но повреждена» от «метки тут нет»: во втором
        случае честнее говорить про размер и пережатие, а не про повреждение.
        """
        return self.mismatched <= MAX_DAMAGED_BITS


def kmeans_threshold(values: Sequence[float]) -> float:
    """Граница между нулями и единицами — тем же способом, что и в библиотеке.

    Повторяет ``blind_watermark.bwm_core.one_dim_kmeans``, чтобы округление
    здесь и внутри библиотеки давало одинаковый результат. Отличие одно: вырожденный
    случай, когда все значения по одну сторону, тут не роняет процесс делением
    на пустое среднее, а просто останавливает подбор.
    """
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        return 0.0

    center = [float(data.min()), float(data.max())]
    threshold = (center[0] + center[1]) / 2
    for _ in range(300):
        threshold = (center[0] + center[1]) / 2
        upper = data > threshold
        if not upper.any() or upper.all():
            break
        center = [float(data[~upper].mean()), float(data[upper].mean())]
        if abs((center[0] + center[1]) / 2 - threshold) < 1e-6:
            threshold = (center[0] + center[1]) / 2
            break
    return threshold


def bits_of(payload: str) -> tuple[bool, ...]:
    """Биты метки в том же порядке, в каком их вшивает библиотека."""
    return tuple(char == "1" for char in bin(int(payload.encode("utf-8").hex(), base=16))[2:])


def decode(read: SoftRead) -> str | None:
    """Округлить биты и собрать строку — ровно то, что делает библиотека.

    Нужно для журнала и для случая, когда сверять не с чем: важно сохранить
    сырое прочитанное значение, даже если оно оказалось мусором.
    """
    if not read.values:
        return None
    bits = "".join("1" if value > read.threshold else "0" for value in read.values)
    width = 2 * ((len(bits) + 7) // 8)
    try:
        return bytes.fromhex(f"{int(bits, base=2):x}".zfill(width)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def match(read: SoftRead, issued: Iterable[str]) -> Verdict | None:
    """Найти среди выданных копий ту, что лучше всех объясняет прочитанное.

    ``None`` — сверять не с чем: ни одна выданная метка не подошла по длине.
    """
    centred = [value - read.threshold for value in read.values]
    scored: list[tuple[float, int, str]] = []
    for payload in dict.fromkeys(issued):  # без повторов, порядок сохраняется
        bits = bits_of(payload)
        if len(bits) != len(centred):
            continue
        score = sum(
            offset if bit else -offset for bit, offset in zip(bits, centred, strict=True)
        )
        mismatched = sum((offset > 0) != bit for bit, offset in zip(bits, centred, strict=True))
        scored.append((score, mismatched, payload))

    if not scored:
        return None

    scored.sort(key=lambda item: (-item[0], item[2]))
    score, mismatched, payload = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None

    # Отрыв нормируется на удвоенный разброс: две метки, различающиеся одним
    # битом, расходятся ровно на 2·|значение − граница|, поэтому в этих единицах
    # margin читается как «уверенность спорного бита».
    margin = (
        float("inf") if runner_up is None else (score - runner_up[0]) / (2 * read.spread)
    )

    return Verdict(
        payload=payload,
        mismatched=mismatched,
        margin=margin,
        runner_up=runner_up[2] if runner_up else None,
        weakest_bit=read.weakest_bit,
    )


def near_misses(read: SoftRead, issued: Iterable[str], *, limit: int = 3) -> list[str]:
    """Метки, до которых прочитанному не хватило одного-двух бит.

    Показываются автору вместо имени, когда назвать человека нельзя: это
    честная формулировка «вот кто мог быть, но метка повреждена».
    """
    centred = [value - read.threshold for value in read.values]
    close: list[tuple[int, float, str]] = []
    for payload in dict.fromkeys(issued):
        bits = bits_of(payload)
        if len(bits) != len(centred):
            continue
        mismatched = sum((offset > 0) != bit for bit, offset in zip(bits, centred, strict=True))
        if mismatched > 2:
            continue
        score = sum(
            offset if bit else -offset for bit, offset in zip(bits, centred, strict=True)
        )
        close.append((mismatched, -score, payload))
    # Порядок — по тому, насколько метка объясняет прочитанное, а не по алфавиту:
    # иначе настоящий владелец вылетает из короткого списка из-за своего номера.
    close.sort()
    return [payload for _, _, payload in close[:limit]]
