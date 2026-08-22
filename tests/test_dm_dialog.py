"""Бот в личке: когда он говорит и, что важнее, когда молчит.

Проверять это перепиской на боевом аккаунте нельзя — ошибка здесь означает,
что бот влез репликой в живой разговор автора с покупателем. Поэтому решение
вынесено в чистую функцию, а сюда — все случаи, ради которых оно написано.
"""

from __future__ import annotations

from datetime import timedelta
from html.parser import HTMLParser

from app.bots.dm import faq
from app.bots.dm.dialog import (
    CALL_COOLDOWN,
    MUTE,
    NUDGE_COOLDOWN,
    Reply,
    decide,
    may_call,
    viewed_keys,
)
from app.bots.dm.handlers import after_answer, card, from_owner_side, menu
from app.db import DmDialog, utcnow


def talk(**fields: object) -> DmDialog:
    fields.setdefault("viewed", "")
    return DmDialog(chat_id=777, connection_id="conn", **fields)


def test_neznakomets_poluchaet_privetstvie() -> None:
    """Первое сообщение от человека, которого мы ещё не видели."""
    assert decide(None) is Reply.GREET


def test_posle_privetstviya_pokazyvaem_menyu_bez_zdravstvuyte() -> None:
    """Второй вопрос свободным текстом — меню, но здороваться второй раз незачем."""
    now = utcnow()
    dialog = talk(greeted_at=now - timedelta(minutes=5))

    assert decide(dialog, now) is Reply.NUDGE


def test_pyat_soobscheniy_podryad_ne_dayut_pyat_menyu() -> None:
    """Человек, который пишет очередью, не должен получить частокол из меню."""
    now = utcnow()
    dialog = talk(greeted_at=now - timedelta(hours=1), nudged_at=now - timedelta(minutes=1))

    assert decide(dialog, now) is Reply.SILENT


def test_cherez_polden_menyu_snova_umestno() -> None:
    """Пауза кончилась — на новый вопрос снова отвечаем списком."""
    now = utcnow()
    dialog = talk(
        greeted_at=now - timedelta(days=1),
        nudged_at=now - NUDGE_COOLDOWN - timedelta(minutes=1),
    )

    assert decide(dialog, now) is Reply.NUDGE


def test_kogda_avtor_otvetila_sama_bot_molchit() -> None:
    """Главное правило: живой человек в переписке отменяет бота."""
    now = utcnow()
    dialog = talk(greeted_at=now - timedelta(hours=2), muted_until=now + MUTE)

    assert decide(dialog, now) is Reply.SILENT


def test_molchanie_ne_vechnoe_i_razgovor_nachinaetsya_zanovo() -> None:
    """Через месяц тишины это уже другой разговор — и приветствие уместно.

    Ответить тут «такого вопроса нет в списке» было бы репликой на сообщение,
    которого не было: человек написал впервые за месяц.
    """
    now = utcnow()
    dialog = talk(
        greeted_at=now - MUTE - timedelta(days=2),
        nudged_at=now - MUTE - timedelta(days=2),
        muted_until=now - timedelta(minutes=1),
    )

    assert decide(dialog, now) is Reply.GREET


def test_avtora_ne_dergaem_dvazhdy_ot_odnogo_nazhatiya() -> None:
    """Двойное нажатие «Позвать Елену» не должно давать две карточки."""
    now = utcnow()

    assert may_call(None, now)
    assert not may_call(talk(called_at=now - timedelta(minutes=1)), now)
    assert may_call(talk(called_at=now - CALL_COOLDOWN - timedelta(minutes=1)), now)


def test_kazhdaya_knopka_menyu_imeet_otvet() -> None:
    """Кнопка без ответа — это молчание в ответ на нажатие.

    Ловит опечатку в ключе: тексты правит автор, и разъехаться они могут
    незаметно, потому что кнопка при этом продолжает рисоваться.
    """
    keys = [
        button.callback_data.split(":", 1)[1]
        for row in menu().inline_keyboard
        for button in row
    ]

    assert keys[-1] == faq.CALL_HUMAN, "вызов человека должен быть последней кнопкой"
    for key in keys[:-1]:
        assert key in faq.BY_KEY


def test_knopki_ne_dlinnee_ekrana() -> None:
    """Длинная надпись обрезается Telegram многоточием — вопрос становится неясен."""
    for answer in faq.FAQ:
        assert len(answer.button) <= 40, answer.key


def test_diskleymer_tam_gde_govorim_o_rynke() -> None:
    """Ответы про результаты и инструменты обязаны нести оговорку про ИИР."""
    for key in ("difference", "self_learning", "timeframe", "statement", "grail"):
        assert faq.DISCLAIMER in faq.BY_KEY[key].body()

    # А там, где речь о технике, приписка только мешает.
    assert faq.DISCLAIMER not in faq.BY_KEY["trial"].body()


def test_soobschenie_ot_sobesednika_ne_schitaetsya_otvetom_avtora() -> None:
    """Входящее: отправитель и чат — один и тот же человек."""

    class Fake:
        from_user = type("U", (), {"id": 555})()
        chat = type("C", (), {"id": 555})()

    assert not from_owner_side(Fake())  # type: ignore[arg-type]


def test_soobschenie_avtora_uznaetsya_po_nesovpadeniyu() -> None:
    """Исходящее: чат остался собеседником, а отправитель — автор."""

    class Fake:
        from_user = type("U", (), {"id": 111})()
        chat = type("C", (), {"id": 555})()

    assert from_owner_side(Fake())  # type: ignore[arg-type]


def test_kartochka_ekraniruet_imya() -> None:
    """Имя приходит из Telegram и содержит что угодно, включая разметку."""
    user = type("U", (), {"full_name": "Рога & <b>Копыта</b>", "username": None, "id": 42})()

    text = card(user, [])  # type: ignore[arg-type]

    assert "&amp;" in text
    assert "<b>Копыта" not in text
    assert "Меню не открывал" in text


def test_kartochka_pokazyvaet_chto_chelovek_uzhe_prochital() -> None:
    """Автор должна видеть контекст, чтобы не пересказывать то же самое."""
    user = type("U", (), {"full_name": "Роман", "username": "roman", "id": 42})()

    text = card(user, ["trial", "difference"])  # type: ignore[arg-type]

    assert faq.BY_KEY["trial"].button in text
    assert faq.BY_KEY["difference"].button in text
    assert "@roman" in text


def test_prochitannoe_chitaetsya_iz_stroki() -> None:
    assert viewed_keys(None) == []
    assert viewed_keys(talk()) == []
    assert viewed_keys(talk(viewed="trial,course")) == ["trial", "course"]


def parse(text: str) -> list[str]:
    """Разобрать разметку ответа, вернуть использованные теги.

    Кидает ``AssertionError``, если теги не закрыты или закрыты не в том
    порядке. Для Telegram это не мелочь: на кривой разметке он отвергает
    сообщение целиком, и бот молча не отвечает вообще ничего.
    """
    stack: list[str] = []
    used: list[str] = []

    class Reader(HTMLParser):
        def handle_starttag(self, tag: str, attrs: object) -> None:
            stack.append(tag)
            used.append(tag)

        def handle_endtag(self, tag: str) -> None:
            assert stack, f"закрыт {tag}, который не открывали"
            assert stack.pop() == tag, f"теги перепутаны местами у {tag}"

    Reader(convert_charrefs=True).feed(text)
    assert not stack, f"не закрыты теги: {stack}"
    return used


def test_razmetka_otvetov_ne_lomaet_soobschenie() -> None:
    """Каждый ответ, приветствие и подсказка обязаны быть валидным HTML."""
    texts = [answer.body() for answer in faq.FAQ]
    texts += [faq.GREETING, faq.NUDGE, faq.CALL_HUMAN_REPLY]

    allowed = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}
    for text in texts:
        assert set(parse(text)) <= allowed, text[:60]


def test_otvety_vlezayut_v_odno_soobschenie() -> None:
    """Ответ длиннее лимита Telegram просто не уйдёт."""
    for answer in faq.FAQ:
        assert len(answer.body()) < 4096, answer.key


def keys_of(keyboard) -> list[str]:
    return [
        button.callback_data.split(":", 1)[1]
        for row in keyboard.inline_keyboard
        for button in row
    ]


def test_pod_otvetom_ne_ves_spisok_a_dve_knopki() -> None:
    """Пришло от автора после первого дня работы.

    Под готовым ответом снова разворачивалось полное меню: сам ответ уезжал
    вверх, а человек видел те же десять кнопок, будто ничего не выбирал.
    Нужны ровно две — вернуться к вопросам и позвать человека.
    """
    keys = keys_of(after_answer())

    assert keys == [faq.BACK, faq.CALL_HUMAN]


def test_knopka_nazad_ne_putaetsya_s_otvetom() -> None:
    """У «Других вопросов» не должно быть ответа в списке.

    Иначе обработчик показал бы её как обычный пункт, и возврат к меню
    молча превратился бы в ещё один ответ.
    """
    assert faq.BACK not in faq.BY_KEY
    assert faq.CALL_HUMAN not in faq.BY_KEY


def test_polnoe_menyu_ostalos_polnym() -> None:
    """Само меню не должно было пострадать: там по-прежнему все вопросы."""
    keys = keys_of(menu())

    assert keys[:-1] == [answer.key for answer in faq.FAQ]
    assert keys[-1] == faq.CALL_HUMAN
    assert faq.BACK not in keys, "в полном меню возвращаться некуда"
