import pytest

from bot.text import split_message


def test_short_message_single_chunk():
    assert split_message("哼") == ["哼"]


def test_empty_message():
    assert split_message("   ") == []


def test_long_message_split_under_limit():
    text = "啊" * 4500
    chunks = split_message(text)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text
    assert len(chunks) == 3


def test_prefers_newline_boundaries():
    para = "x" * 1500
    text = f"{para}\n{para}\n{para}"
    chunks = split_message(text)
    assert chunks == [para, para, para]


def test_prefers_space_when_no_newline():
    words = " ".join(["word"] * 1000)  # 4999 字元
    chunks = split_message(words)
    assert all(len(c) <= 2000 for c in chunks)
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)
    assert " ".join(chunks).split() == words.split()


def test_custom_limit():
    assert split_message("abcdef", limit=2) == ["ab", "cd", "ef"]
    with pytest.raises(ValueError):
        split_message("a", limit=0)
