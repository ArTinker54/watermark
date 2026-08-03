"""Разрез длинной подписи по границам, не ломающим разметку.

Подпись к материалу — это HTML от автора плюс невидимые символы метки. Резать
её ровно по лимиту нельзя: разрез посреди тега даёт битую разметку, разрез между
открывающим и закрывающим — незакрытую. Telegram отвергает и то и другое, причём
у всех получателей сразу.
"""

from __future__ import annotations

import re

import pytest

from app.utils import TEXT_LIMIT, split_html

_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*>")


def balanced(chunk: str) -> bool:
    """Все ли теги в куске закрыты и закрыты в правильном порядке."""
    stack: list[str] = []
    for match in _TAG.finditer(chunk):
        name = match.group(2).lower()
        if match.group(1):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def visible(text: str) -> str:
    """Текст без разметки — то, что в итоге прочитает человек."""
    return _TAG.sub("", text)


def test_short_text_is_not_touched() -> None:
    assert split_html("<b>коротко</b>") == ["<b>коротко</b>"]


def test_never_cuts_inside_a_tag() -> None:
    """Разрез внутри «<b>» превратил бы тег в мусор."""
    text = ("слово " * 900) + "<b>жирный кусок</b>" + (" ещё " * 900)
    for chunk in split_html(text):
        assert "<" not in chunk.replace("<b>", "").replace("</b>", "")


def test_open_tag_is_closed_and_reopened() -> None:
    """Форматирование, начатое до разреза, продолжается после него."""
    text = "<b>" + ("длинное слово " * 500) + "</b>"
    chunks = split_html(text)

    assert len(chunks) > 1, "текст обязан был разрезаться"
    for chunk in chunks:
        assert balanced(chunk), chunk
        assert chunk.startswith("<b>") and chunk.endswith("</b>")


def test_nested_tags_survive_the_cut() -> None:
    text = "<b>" + ("раз " * 600) + "<i>" + ("два " * 600) + "</i>" + ("три " * 600) + "</b>"
    chunks = split_html(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert balanced(chunk), chunk
    # Кусок, попавший на вложенный участок, обязан нести оба тега.
    assert any("<b>" in chunk and "<i>" in chunk for chunk in chunks)


def test_nothing_is_lost() -> None:
    """Видимый текст сохраняется целиком, до последнего слова."""
    text = "<b>" + ("слово " * 400) + "</b>\n\n<i>" + ("другое " * 400) + "</i>"
    joined = "".join(visible(chunk) for chunk in split_html(text))
    assert joined.replace(" ", "").replace("\n", "") == (
        visible(text).replace(" ", "").replace("\n", "")
    )


def test_every_chunk_fits_the_limit() -> None:
    text = "<b>" + ("слово " * 3000) + "</b>"
    assert all(len(chunk) <= TEXT_LIMIT for chunk in split_html(text))


def test_link_attributes_are_preserved_after_the_cut() -> None:
    text = '<a href="https://example.com/x">' + ("текст ссылки " * 400) + "</a>"
    chunks = split_html(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert balanced(chunk)
        assert 'href="https://example.com/x"' in chunk


def test_solid_text_without_spaces_is_still_split() -> None:
    """Полотно без пробелов резать негде — но оно всё равно обязано уехать."""
    chunks = split_html("<b>" + ("я" * 9000) + "</b>")
    assert all(len(chunk) <= TEXT_LIMIT for chunk in chunks)
    assert "".join(visible(chunk) for chunk in chunks) == "я" * 9000


def test_no_empty_messages() -> None:
    """Кусок из одних тегов Telegram отвергает — выпускать такой нельзя."""
    text = "<b>" + ("слово " * 700) + "</b>" + "<i>" + ("ещё " * 700) + "</i>"
    for chunk in split_html(text):
        assert visible(chunk).strip(), chunk


@pytest.mark.parametrize("limit", [64, 128, 512])
def test_small_limits_terminate(limit: int) -> None:
    """Мелкий лимит не должен зацикливать разрез."""
    text = "<b>" + ("слово " * 200) + "</b>"
    chunks = split_html(text, limit=limit)
    assert chunks
    assert all(balanced(chunk) for chunk in chunks)


def test_unbalanced_markup_does_not_crash() -> None:
    """Кривая разметка от автора не повод ронять рассылку."""
    chunks = split_html("</b>" + ("текст " * 900) + "<i>")
    assert chunks
