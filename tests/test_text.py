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


def test_to_traditional_fixes_stray_simplified():
    from bot.text import to_traditional

    assert to_traditional("徒町聽到的时候，差點哭出來") == "徒町聽到的時候，差點哭出來"
    assert to_traditional("めぐ还是很吵，前几天才讲过话") == "めぐ還是很吵，前几天才講過話"


def test_to_traditional_keeps_traditional_and_japanese():
    from bot.text import to_traditional

    text = "蓮ノ空女学院の学園祭、映画を見に来た。我們沒有後悔，茶几裡的著作。"
    assert to_traditional(text) == text
