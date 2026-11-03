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

#: Сколько помех в НЕопознающих битах ещё позволяет называть человека.
#:
#: Опознающих бит мало — на сотне участников девять из 95. Значит случайный
#: мусор попадает в чью-то комбинацию этих девяти примерно в одном случае из
#: пяти, и одного лишь совпадения по ним мало: нужен признак, что читали
#: настоящую метку, а не шум. Им и служит общее число расхождений — у мусора
#: оно около 47, у настоящей метки с единичной помехой единицы.
#:
#: Четыре, а не восемь: чем больше помех всего, тем вероятнее, что одна из них
#: села на опознающий бит и метка стала соседской. При четырёх на опознающие
#: биты приходится в среднем 0.4 ошибки, при восьми уже 0.8.
MAX_NOISE_BITS: Final[int] = 4


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
    runner_up_mismatched: int | None
    """На сколько бит разошлось со ВТОРОЙ по близости копией.

    Вместе с ``mismatched`` это и есть цена ошибки чтения: если ближайшая
    разошлась на 8 бит, а следующая на 9, то одного лишнего сбоя хватило бы,
    чтобы ответ поменялся на другого человека.
    """

    mismatched_key: int
    """Сколько разошлось среди бит, которыми копии вообще отличаются друг от друга.

    Именно это число решает, кто перед нами. В метке ``V|uid|lesson`` почти всё
    одинаково у всех получателей одного урока: буква, разделители, номер урока
    и даже старшие половины цифр номера. На сотне участников из 95 бит людей
    различают девять — остальные 86 у всех совпадают.

    Сбой в этих 86 битах не может превратить одного участника в другого: там не
    записано, кто это. Считать его наравне с остальными — значит отказываться
    называть человека из-за помехи, которая ни на что не влияет.
    """

    weakest_bit: float

    @property
    def trustworthy(self) -> bool:
        """Можно ли называть человека.

        Три условия. Первое: опознающие биты прочитаны без единой ошибки — на
        них и держится ответ. Второе: следующий кандидат заметно хуже; это
        защита от одиночного сбоя в опознающем бите, из-за которого метка
        становится соседской. Третье: помех в остальных битах немного, иначе
        чтение шумное целиком и верить его опознающим битам не за что.

        Раньше первое условие требовало совпадения по ВСЕМ битам. На боевой
        трассировке это стоило имени: метка прочиталась как ``V|0195x00143``,
        то есть с единственным сбоем в разделителе — бите, одинаковом у всех
        106 выданных копий. Все девять опознающих бит при этом были верны.
        """
        return (
            self.mismatched_key == 0
            and self.mismatched <= MAX_NOISE_BITS
            and self.margin >= MIN_MARGIN
        )

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


def varying_positions(payloads: Sequence[tuple[bool, ...]]) -> tuple[int, ...]:
    """Позиции бит, которыми выданные копии вообще отличаются друг от друга.

    Всё остальное — буква, разделители, номер урока, старшие половины цифр —
    у получателей одного урока совпадает и потому никого ни с кем перепутать
    не может.
    """
    if not payloads:
        return ()
    return tuple(
        index
        for index in range(len(payloads[0]))
        if len({bits[index] for bits in payloads}) > 1
    )


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
    candidates = [
        (payload, bits_of(payload))
        for payload in dict.fromkeys(issued)  # без повторов, порядок сохраняется
    ]
    candidates = [item for item in candidates if len(item[1]) == len(centred)]
    # Опознающие биты считаются по всему списку выданных копий, а не по одной:
    # «различает» — это свойство набора, а не отдельной метки.
    key_positions = varying_positions([bits for _, bits in candidates])

    scored: list[tuple[float, int, int, str]] = []
    for payload, bits in candidates:
        score = sum(
            offset if bit else -offset for bit, offset in zip(bits, centred, strict=True)
        )
        wrong = [
            index
            for index, (bit, offset) in enumerate(zip(bits, centred, strict=True))
            if (offset > 0) != bit
        ]
        mismatched = len(wrong)
        mismatched_key = sum(1 for index in wrong if index in set(key_positions))
        scored.append((score, mismatched, mismatched_key, payload))

    if not scored:
        return None

    scored.sort(key=lambda item: (-item[0], item[3]))
    score, mismatched, mismatched_key, payload = scored[0]
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
        runner_up=runner_up[3] if runner_up else None,
        runner_up_mismatched=runner_up[1] if runner_up else None,
        mismatched_key=mismatched_key,
        weakest_bit=read.weakest_bit,
    )


@dataclass(frozen=True, slots=True)
class NearMiss:
    """Выданная копия, которая могла быть источником повреждённой метки."""

    payload: str
    gap: float
    """Насколько эта копия объясняет прочитанное хуже первой в круге.

    В тех же единицах, что и ``Verdict.margin``: доли уверенности одного бита.
    Ниже ``MIN_MARGIN`` копия от первой не отличается — между ними выбирает
    один едва прочитанный бит.
    """

    @property
    def level_with_first(self) -> bool:
        return self.gap < MIN_MARGIN


@dataclass(frozen=True, slots=True)
class Circle:
    """Круг копий, из которых могла получиться повреждённая метка."""

    members: tuple[NearMiss, ...]
    level_total: int
    """Сколько выданных копий вровень с первой, считая её саму.

    Может быть больше, чем показано: круг ограничен, а молча обрезанный
    список выглядит полным.
    """


def near_misses(read: SoftRead, issued: Iterable[str], *, limit: int = 5) -> Circle:
    """Копии, которые лучше всех объясняют прочитанное, — по порядку.

    Показывается автору вместо имени, когда назвать человека нельзя: это
    честная формулировка «вот кто мог быть, но метка повреждена».

    Порядок — только по мягкому совпадению, без отбора по числу разошедшихся
    бит. Оба прежних способа выбрасывали настоящего владельца:

    * отбор по общему числу расхождений не больше двух давал пустой круг, едва
      помехи ложились в биты, одинаковые у всех копий (боевой урок #322: шесть
      таких помех, и «сузить не удалось» у всех 177 копий);
    * отбор окном по опознающим битам терял владельца, когда два слабых сбоя
      превращали метку в чужую выданную копию: у владельца два «мимо», и в
      окно он не проходил, хотя по совпадению стоял вторым, почти вровень.

    Число разошедшихся бит считает слабый и уверенный сбой одинаково, а
    совпадение — нет. Поэтому решает оно.
    """
    centred = [value - read.threshold for value in read.values]
    scored: list[tuple[float, str]] = []
    for payload in dict.fromkeys(issued):
        bits = bits_of(payload)
        if len(bits) != len(centred):
            continue
        score = sum(
            offset if bit else -offset for bit, offset in zip(bits, centred, strict=True)
        )
        scored.append((score, payload))
    if not scored:
        return Circle(members=(), level_total=0)

    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0][0]
    unit = 2 * read.spread
    gaps = [((best - score) / unit, payload) for score, payload in scored]
    return Circle(
        members=tuple(NearMiss(payload=payload, gap=gap) for gap, payload in gaps[:limit]),
        level_total=sum(1 for gap, _ in gaps if gap < MIN_MARGIN),
    )

