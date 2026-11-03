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
    candidates = [
        item.payload
        for item in near_misses(with_bit_at(TRUTH, differing, offset=0.02), ISSUED).members
    ]
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


def test_krug_ne_pustoy_pri_pomehah_v_obschih_bitah() -> None:
    """Боевой случай урока #322: «ближайшая разошлась на 1 опознающий бит,
    а всего мимо 7» — и при этом «кандидаты: сузить не удалось».

    Шесть помех легли в биты, одинаковые у всех копий, ещё одна — в опознающий,
    причём слабо, как это и бывает при пережатии. Круг обязан быть, и настоящий
    владелец в нём — среди первых: помехи в общих битах никого не отличают.
    """
    read = noisy(WIDE_TRUTH, weak=[key_bits()[3]])

    verdict = match(read, WIDE)
    payloads = [item.payload for item in near_misses(read, WIDE).members]

    assert verdict is not None and not verdict.trustworthy, "назвать по-прежнему нельзя"
    assert verdict.mismatched > 2, "общих помех больше двух — старый отбор давал пустоту"
    assert payloads, "круг не пустой"
    assert WIDE_TRUTH in payloads[:2], "настоящий владелец среди первых"


def noisy(
    payload: str, *, weak: list[int], offset: float = 0.03, noise: int = 6
) -> SoftRead:
    """Как в бою: уверенные помехи в общих битах и слабо сбитые ``weak``."""
    bits = list(bits_of(payload))
    for index in [index for index in range(95) if index not in key_bits()][:noise]:
        bits[index] = not bits[index]
    values = [0.5 + (0.4 if bit else -0.4) for bit in bits]
    for index in weak:
        values[index] = 0.5 + (-offset if bits[index] else offset)
    return SoftRead(values=tuple(values), threshold=kmeans_threshold(values))


def lands_on_issued(*flips: int) -> bool:
    bits = list(bits_of(WIDE_TRUTH))
    for index in flips:
        bits[index] = not bits[index]
    return tuple(bits) in {bits_of(item) for item in WIDE}


@pytest.mark.parametrize("offset", [0.03, 0.035, 0.05])
def test_vladelec_ne_vypadaet_kogda_dva_sboya_dayut_chuzhuyu_kopiyu(offset: float) -> None:
    """Найдено проверкой: два слабых сбоя превратили метку в чужую выданную.

    У владельца тогда два «мимо», и отбор окном по числу бит его выбрасывал,
    подставляя людей со сбоем уверенно прочитанного бита. По совпадению же
    он второй, почти вровень с победителем.

    Вторая проверка нашла и следующий слой: «вровень» считалось по сумме
    отставания, и уже при 0.035 два едва прочитанных бита в сумме перевешивали
    порог — владелец оставался без пометки прямо под чужой копией.
    """
    pair = next(
        (a, b)
        for position, a in enumerate(key_bits())
        for b in key_bits()[position + 1 :]
        if lands_on_issued(a, b)
    )
    read = noisy(WIDE_TRUTH, weak=list(pair), offset=offset)

    circle = near_misses(read, WIDE)
    owner = next(item for item in circle.members if item.payload == WIDE_TRUTH)

    assert circle.members[0].payload != WIDE_TRUTH, "первым встала чужая копия"
    assert owner.level, "и владелец помечен как неотличимый от неё"
    assert circle.members[0].level, "пометка симметрична — у первого тоже"


def test_pervyy_v_kruge_ne_vyglyadit_opoznannym() -> None:
    """Найдено проверкой: один слабый сбой дал ровно чужую выданную метку.

    Первым в круге тогда невиновный. Строка «опознающих бит мимо: 0» читалась
    как «сошлось именно с ним» — теперь такого нет, а владелец рядом помечен
    как неотличимый.
    """
    from app.db import Student
    from app.services.trace import _damaged_note

    shaky = next(index for index in key_bits() if lands_on_issued(index))
    read = noisy(WIDE_TRUTH, weak=[shaky])
    verdict = match(read, WIDE)
    assert verdict is not None and verdict.payload != WIDE_TRUTH
    circle = near_misses(read, WIDE)
    owner = Student(tg_user_id=1, uid=195, full_name="Владелец")

    note = _damaged_note(read, WIDE, verdict, circle, {195: owner})
    first = circle.members[0].payload.split("|")[1]

    assert "мимо: 0" not in note
    assert "опознающие биты сошлись" not in note, "заголовок не намекает на первого"
    assert "первый в списке не обязательно источник" in note
    assert "0195 — Владелец — вровень" in note
    assert f"• {first} — в базе нет — вровень" in note, "первый помечен так же"


@pytest.mark.parametrize("noise", [2, 6])
def test_prichina_otkaza_nazvana_verno(noise: int) -> None:
    """Отказ из-за соседа вровень не выдаётся за «слишком шумное чтение».

    При двух помехах чтение вполне годное — помешал сосед, и говорить про шум
    было бы неправдой. При шести мешают оба, и честнее назвать соседа: его
    видно по пометкам в круге.
    """
    from app.services.trace import _damaged_note

    shaky = next(index for index in key_bits() if lands_on_issued(index))
    read = noisy(WIDE_TRUTH, weak=[shaky], noise=noise)
    verdict = match(read, WIDE)
    assert verdict is not None and verdict.mismatched_key == 0
    assert verdict.margin < MIN_MARGIN

    note = _damaged_note(read, WIDE, verdict, near_misses(read, WIDE), {})

    assert "другая почти так же близка" in note
    assert "шумное" not in note


def test_odna_kopiya_bez_abzaca_pro_poryadok() -> None:
    """Выдана одна копия — круг из одной строки без рассуждений про «первого»."""
    from app.services.trace import _damaged_note

    only = [WIDE_TRUTH]
    read = noisy(WIDE_TRUTH, weak=[])
    verdict = match(read, only)
    assert verdict is not None and not verdict.trustworthy

    note = _damaged_note(read, only, verdict, near_misses(read, only), {})

    assert "Ближе всего прочитанное к копии" in note
    assert "первый в списке" not in note


def test_skrytye_vrovne_ne_teryayutsya_molcha() -> None:
    """Если вровень больше, чем влезло в круг, — так и сказано."""
    from app.services.trace import _damaged_note

    read = noisy(WIDE_TRUTH, weak=list(key_bits()[:4]))
    verdict = match(read, WIDE)
    assert verdict is not None
    circle = near_misses(read, WIDE, limit=2)
    shown = sum(1 for item in circle.members if item.level)
    assert circle.level_total > shown, "опыт поставлен на широкий круг"
    assert WIDE_TRUTH in circle.level_payloads, "в полном списке владелец есть"

    note = _damaged_note(read, WIDE, verdict, circle, {})

    assert f"ещё вровень с ними: {circle.level_total - shown}" in note


def test_krug_s_imenami_ne_lomaet_razmetku() -> None:
    """Имена в круге — из Telegram, а ответ уходит с HTML-разметкой.

    И круг обязан звучать как круг: «вот кто мог быть», а не «вот кто это».
    """
    from app.db import Student
    from app.services.trace import _damaged_note

    read = flipped(WIDE_TRUTH, key_bits()[3])
    verdict = match(read, WIDE)
    assert verdict is not None
    circle = near_misses(read, WIDE)
    student = Student(tg_user_id=1, uid=195, full_name="Рога & <b>Копыта</b>", username="rk")

    note = _damaged_note(read, WIDE, verdict, circle, {195: student})

    assert "0195" in note
    assert "&amp;" in note and "<b>Копыта" not in note
    assert "@rk" in note
    assert "не доказательство" in note
    assert "Назвать участника нельзя" in note


def test_v_krug_ne_popadaet_vsya_tolpa() -> None:
    """Круг — это несколько ближайших, а не полсотни «тоже похожих»."""
    read = flipped(WIDE_TRUTH, *key_bits()[:3])

    assert len(near_misses(read, WIDE).members) <= 5


def test_chistoe_chtenie_nazyvaet_cheloveka() -> None:
    """Проверка на то, что послабление не сломало обычный путь."""
    verdict = match(flipped(WIDE_TRUTH), WIDE)

    assert verdict is not None and verdict.trustworthy
    assert verdict.payload == WIDE_TRUTH
