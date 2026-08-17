"""Сверка прочитанной метки со списком выданных копий.

Главное, что здесь проверяется, — что одиночный сбой бита больше не превращается
в уверенно названное чужое имя. Именно так вела себя старая проверка формата:
строка ``V|0043|00001`` формально правильна, номер урока в ней верный, и дальше
находился реальный ученик 0043 с ровно такой меткой в журнале.
"""

from __future__ import annotations

import pytest

from app.watermark.payload import WM_BIT_LENGTH, Payload
from app.watermark.verify import (
    MAX_NOISE_BITS,
    MIN_MARGIN,
    SoftRead,
    bits_of,
    decode,
    kmeans_threshold,
    match,
    near_misses,
    varying_positions,
)

#: Выданные копии одного урока — как в жизни, подряд идущие номера.
ISSUED = [Payload(uid=uid, lesson_id=1).encode() for uid in range(1, 90)]
TRUTH = Payload(uid=42, lesson_id=1).encode()


def read_of(payload: str, *, strength: float = 1.0) -> SoftRead:
    """Идеальное чтение метки: каждый бит уверенно на своей стороне."""
    values = tuple(0.5 + strength * (0.4 if bit else -0.4) for bit in bits_of(payload))
    return SoftRead(values=values, threshold=kmeans_threshold(values))


def with_bit_at(payload: str, index: int, offset: float) -> SoftRead:
    """То же чтение, но один бит сдвинут на чужую сторону на ``offset``."""
    read = read_of(payload)
    values = list(read.values)
    values[index] = read.threshold + (offset if values[index] < read.threshold else -offset)
    return SoftRead(values=tuple(values), threshold=read.threshold)


def test_clean_read_names_the_right_copy() -> None:
    verdict = match(read_of(TRUTH), ISSUED)
    assert verdict is not None
    assert verdict.payload == TRUTH
    assert verdict.mismatched == 0
    assert verdict.trustworthy


def test_single_bit_error_into_someone_elses_uid_is_refused() -> None:
    """Тот самый дефект: сбой бита даёт валидную метку ЧУЖОГО ученика.

    Берётся позиция, по которой ``V|0042|00001`` отличается от выданной
    ``V|0043|00001`` ровно одним битом, и бит слегка перебрасывается через
    границу — как это делает пережатие. Формат при этом безупречен, номер урока
    верный, ученик 0043 существует. Назвать его нельзя.
    """
    neighbour = Payload(uid=43, lesson_id=1).encode()
    differing = [
        index
        for index, (a, b) in enumerate(zip(bits_of(TRUTH), bits_of(neighbour), strict=True))
        if a != b
    ]
    assert len(differing) == 1, "для чистоты опыта нужен сосед ровно в одном бите"

    read = with_bit_at(TRUTH, differing[0], offset=0.02)
    verdict = match(read, ISSUED)

    assert verdict is not None
    assert verdict.payload == neighbour, "прочиталась именно чужая метка"
    assert verdict.mismatched == 0, "и она совпала точно — формат тут не спасает"
    assert not verdict.trustworthy, "но отрыв мизерный, называть человека нельзя"
    assert verdict.margin < MIN_MARGIN


def test_confident_read_survives_a_far_neighbour() -> None:
    """Уверенное чтение не должно отбрасываться из-за самого факта соседства."""
    verdict = match(read_of(TRUTH), ISSUED)
    assert verdict is not None
    assert verdict.margin >= MIN_MARGIN


def test_unissued_payload_never_wins() -> None:
    """Метка, которой не выдавали, не может стать ответом ни при каком чтении."""
    stranger = Payload(uid=900, lesson_id=1).encode()
    assert stranger not in ISSUED
    verdict = match(read_of(stranger), ISSUED)
    assert verdict is not None
    assert verdict.payload != stranger
    assert verdict.mismatched > 0
    assert not verdict.trustworthy


def test_noise_is_not_mistaken_for_a_damaged_mark() -> None:
    """Чистый шум — это «метки нет», а не «метка повреждена»."""
    values = tuple(0.5 + 0.001 * ((index * 37) % 11 - 5) for index in range(WM_BIT_LENGTH))
    read = SoftRead(values=values, threshold=kmeans_threshold(values))
    verdict = match(read, ISSUED)
    assert verdict is not None
    assert not verdict.trustworthy
    assert not verdict.plausible


def test_decode_returns_the_raw_string_even_when_it_is_garbage() -> None:
    """Сырое чтение сохраняется всегда — это часть доказательной базы."""
    assert decode(read_of(TRUTH)) == TRUTH


def test_near_misses_lists_the_plausible_candidates() -> None:
    neighbour = Payload(uid=43, lesson_id=1).encode()
    differing = next(
        index
        for index, (a, b) in enumerate(zip(bits_of(TRUTH), bits_of(neighbour), strict=True))
        if a != b
    )
    candidates = near_misses(with_bit_at(TRUTH, differing, offset=0.02), ISSUED)
    assert neighbour in candidates
    assert TRUTH in candidates, "настоящий владелец обязан остаться в списке"


@pytest.mark.parametrize("payload", [TRUTH, Payload(uid=1, lesson_id=99999).encode()])
def test_bits_length_matches_the_mark(payload: str) -> None:
    assert len(bits_of(payload)) == WM_BIT_LENGTH


def test_vydannye_metki_lezhat_vplotnuyu_drug_k_drugu() -> None:
    """Свойство формата, из которого следует всё остальное в этом файле.

    ``V|uid|lesson`` не разносит номера по битам: соседние uid отличаются
    одним-двумя битами. Отсюда и запрет называть человека по неточному
    совпадению — ошибка чтения в пару бит превращает метку в соседскую.
    """
    distances = sorted(
        sum(x != y for x, y in zip(bits_of(a), bits_of(b), strict=True))
        for index, a in enumerate(ISSUED)
        for b in ISSUED[index + 1 :]
    )

    assert distances[0] == 1, "нашлись метки, различающиеся одним битом"
    assert distances[len(distances) // 2] <= 6, "медиана расстояния мала"


def test_vidno_naskolko_otstala_sleduyuschaya_kopiya() -> None:
    """Расстояние до второго места — это цена ошибки чтения, и её надо показать.

    Без неё «ближайшая разошлась на 8 бит» звучит как «почти нашли», хотя рядом
    стоит толпа таких же почти совпавших.
    """
    verdict = match(read_of(TRUTH), ISSUED)

    assert verdict is not None
    assert verdict.mismatched == 0
    assert verdict.runner_up_mismatched is not None
    assert verdict.runner_up_mismatched >= 1


#: Номера как в бою: выдача идёт не подряд, часть участников отсеивается.
WIDE = [Payload(uid=uid, lesson_id=143).encode() for uid in (*range(1, 100), *range(150, 202))]
WIDE_TRUTH = Payload(uid=195, lesson_id=143).encode()


def key_bits() -> tuple[int, ...]:
    return varying_positions([bits_of(item) for item in WIDE])


def flipped(payload: str, *positions: int) -> SoftRead:
    """Чтение метки с перевёрнутыми битами в указанных позициях."""
    bits = list(bits_of(payload))
    for index in positions:
        bits[index] = not bits[index]
    values = tuple(0.5 + (0.4 if bit else -0.4) for bit in bits)
    return SoftRead(values=values, threshold=kmeans_threshold(values))


def test_metku_razlichayut_edinicy_bit() -> None:
    """Опознающих бит мало — и это причина всего остального в этом файле.

    В ``V|uid|lesson`` совпадает почти всё: буква, разделители, номер урока и
    старшие половины цифр номера. На полутора сотнях участников людей различают
    девять бит из 95.
    """
    assert len(key_bits()) < 15, "опознающих бит должно быть заметно меньше длины метки"


def test_sboy_v_razdelitele_ne_meshaet_nazvat_cheloveka() -> None:
    """Боевой случай: прочиталось ``V|0195x00143`` — сбой в разделителе.

    Этот бит одинаков у всех выданных копий, перепутать по нему никого нельзя.
    Раньше проверка требовала совпадения по всем 95 битам и отказывалась
    называть участника из-за помехи, которая ни на что не влияет.
    """
    separator = next(index for index in range(95) if index not in key_bits() and index > 40)

    verdict = match(flipped(WIDE_TRUTH, separator), WIDE)

    assert verdict is not None
    assert verdict.payload == WIDE_TRUTH
    assert verdict.mismatched == 1, "один бит всё же разошёлся"
    assert verdict.mismatched_key == 0, "но опознающие биты прочитаны верно"
    assert verdict.trustworthy


def test_sboy_v_opoznayuschem_bite_po_prezhnemu_zapreschaet_imya() -> None:
    """Главная защита не должна ослабнуть: сбой в опознающем бите — это отказ.

    Именно так метка превращается в соседскую, и именно поэтому нельзя просто
    разрешить «почти совпало».
    """
    verdict = match(flipped(WIDE_TRUTH, key_bits()[3]), WIDE)

    assert verdict is not None
    assert verdict.payload != WIDE_TRUTH, "победила чужая метка — так и бывает"
    assert verdict.mismatched_key == 1
    assert not verdict.trustworthy


def test_shumnoe_chtenie_ne_daet_imeni_dazhe_pri_vernyh_opoznayuschih() -> None:
    """Много помех — значит чтению нельзя верить целиком.

    Опознающих бит девять, случайный мусор попадает в чью-то комбинацию
    примерно в одном случае из пяти. Поэтому совпадения по ним мало: нужен
    признак, что читали настоящую метку.
    """
    noise = [index for index in range(95) if index not in key_bits()][:6]

    verdict = match(flipped(WIDE_TRUTH, *noise), WIDE)

    assert verdict is not None
    assert verdict.mismatched_key == 0
    assert verdict.mismatched > MAX_NOISE_BITS
    assert not verdict.trustworthy


def test_chistoe_chtenie_nazyvaet_cheloveka() -> None:
    """Проверка на то, что послабление не сломало обычный путь."""
    verdict = match(flipped(WIDE_TRUTH), WIDE)

    assert verdict is not None and verdict.trustworthy
    assert verdict.payload == WIDE_TRUTH
