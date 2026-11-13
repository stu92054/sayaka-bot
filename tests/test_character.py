"""角色資料夾（CHARACTER_DIR）：persona、replies、lore、memories 的預設位置與 character.toml。"""
from pathlib import Path

import pytest

from bot.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_default_character_is_sayaka():
    c = Config.from_env({})
    assert c.character_dir == Path("characters/sayaka")
    assert c.persona_path == Path("characters/sayaka/persona.md")
    assert c.replies_path == Path("characters/sayaka/replies.toml")
    assert c.bot_display_name == "沙耶香"
    # 沒指定 CHARACTER_DIR 時沿用舊的資料庫檔名，既有部署不用搬資料
    assert c.db_path == Path("data/bot.sqlite3")


def test_bundled_character_files_exist():
    d = ROOT / "characters/sayaka"
    for name in ("persona.md", "replies.toml", "character.toml", "lore", "memories"):
        assert (d / name).exists(), name


def test_bundled_kosuzu_loads():
    from bot.lore import Lore
    from bot.persona import Persona
    from bot.replies import Replies

    d = ROOT / "characters/kosuzu"
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.bot_display_name == "小鈴" and "徒町小鈴" in c.bot_aliases
    assert "徒町" in Persona(c.persona_path).text
    assert Replies(c.replies_path).warnings == []
    lore = Lore(c.lore_dir)
    assert lore.warnings == []
    assert lore.files == ["共通_01_社團與成員.md", "01_個人設定.md", "02_人物關係.md", "03_重要事件時間線.md", "04_日常與小事.md", "05_With×MEETS.md"]
    assert 10_000 < len(lore.text) < 35_000  # 含共通 lore（約 3 千字）
    # 資料包的出處標記、內部路徑不應該進到 lore
    for junk in ("〔", "台本", "百科", "raw_", ".txt", "間章"):
        assert junk not in lore.text, junk



def test_bundled_tsuzuri_loads():
    from bot.lore import Lore
    from bot.persona import Persona
    from bot.replies import Replies

    d = ROOT / "characters/tsuzuri"
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.bot_display_name == "綴理" and "夕霧綴理" in c.bot_aliases
    assert "自稱「我」" in Persona(c.persona_path).text
    assert Replies(c.replies_path).warnings == []
    lore = Lore(c.lore_dir)
    assert lore.warnings == []
    assert lore.files == ["共通_01_社團與成員.md", "01_個人設定.md", "02_人物關係.md", "03_重要事件時間線.md", "04_日常與小事.md", "05_With×MEETS.md"]
    assert 10_000 < len(lore.text) < 35_000  # 含共通 lore（約 3 千字）
    # 資料包的出處標記、內部路徑、OCR 來源不應該進到 lore
    for junk in ("〔", "台本", "百科", "raw_", ".txt", "間章", "中集", "Starsness", "劇場版", "scripts/", "OCR"):
        assert junk not in lore.text, junk


def test_bundled_kozue_loads():
    """梢：lore 01–05（2026-11-14 加上卡片語音 04、With×MEETS 05）。"""
    from bot.lore import Lore
    from bot.persona import Persona
    from bot.replies import Replies

    d = ROOT / "characters/kozue"
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.bot_display_name == "梢" and "乙宗梢" in c.bot_aliases
    assert "自稱「我」" in Persona(c.persona_path).text
    assert Replies(c.replies_path).warnings == []
    lore = Lore(c.lore_dir)
    assert lore.warnings == []
    assert lore.files == ["共通_01_社團與成員.md", "01_個人設定.md", "02_人物關係.md", "03_重要事件時間線.md",
                          "04_日常與小事.md", "05_With×MEETS.md"]
    assert 10_000 < len(lore.text) < 35_000  # 含共通 lore（約 2,400 字）
    for junk in ("〔", "台本", "百科", "raw_", ".txt", "間章", "中集", "Starsness", "劇場版", "scripts/", "OCR",
                 "待核對", "待補", "BD 特典", "入場者特典", "蓮ノート", "`", "（直播）", "直播、20"):
        assert junk not in lore.text, junk


@pytest.mark.parametrize("key, name, full, self_word", [
    ("ginko", "吟子", "百生吟子", "自稱「我」"),
])
def test_bundled_cerise_members_load(key, name, full, self_word):
    """梢、吟子、花帆（2026-11-13 加入）：目前只有 lore 01–03，卡片語音、With×MEETS、歌曲卡還沒做。"""
    from bot.lore import Lore
    from bot.persona import Persona
    from bot.replies import Replies

    d = ROOT / f"characters/{key}"
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.bot_display_name == name and full in c.bot_aliases
    assert self_word in Persona(c.persona_path).text
    assert Replies(c.replies_path).warnings == []
    lore = Lore(c.lore_dir)
    assert lore.warnings == []
    assert lore.files == ["共通_01_社團與成員.md", "01_個人設定.md", "02_人物關係.md", "03_重要事件時間線.md"]
    assert 10_000 < len(lore.text) < 35_000  # 含共通 lore（約 2,400 字）
    # 資料包的出處標記、內部路徑、作品名不應該進到 lore
    for junk in ("〔", "台本", "百科", "raw_", ".txt", "間章", "中集", "Starsness", "劇場版", "scripts/", "OCR",
                 "待核對", "待補", "BD 特典", "入場者特典", "蓮ノート", "`"):
        assert junk not in lore.text, (key, junk)


def test_bundled_tsuzuri_meets_cards():
    import re

    from bot.memories import Memories

    m = Memories(ROOT / "characters/tsuzuri/memories")
    meets = [c for c in m.cards if re.match(r"\d{4}-\d{2}-\d{2}『", c.title)]
    assert len(meets) == 110
    text = "\n".join(c.text for c in meets)
    # 部室的雞玩偶統一寫成原作的「ぺきんだっく」；聽錯或觀眾的寫法只留在 aliases.txt
    assert "ぺきんだっく" in text and not re.search(r"ペキンダック|北京ダック|北京烤鴨|スケープゴート", text)
    assert "ぺきんだっく" in m.search("北京烤鴨 玩偶")[0].text
    assert m.search("合宿ラジオ")[0].title == "2023-08-12『合宿ラジオ』"
    assert m.search("ボクの最後のWith×MEETS")[0].title == "2025-03-24『ボクの最後のWith×MEETSだよ。』"


def test_bundled_tsuzuri_song_cards():
    from bot.memories import Memories

    m = Memories(ROOT / "characters/tsuzuri/memories")
    assert m.warnings == [] and len(m.cards) > 200
    text = "\n".join(c.text for c in m.cards)
    # 歌曲卡是以綴理的角度產生的，不能殘留其他角色版的標記
    assert "綴理有唱" in text and "小鈴有唱" not in text and "沙耶香有唱" not in text
    assert m.search("綴理 唱過 哪些歌")[0].title == "歌單：綴理唱過的歌（總覽）"
    assert m.search("COMPASS")[0].title == "歌曲『COMPASS』"
    # 維基看不出來的初演：102 期沙知和綴理的 DOLLCHESTRA
    assert "沙知和綴理兩人的 DOLLCHESTRA" in m.search("Sparkly Spot")[0].text


def test_bundled_kosuzu_song_cards():
    from bot.memories import Memories

    m = Memories(ROOT / "characters/kosuzu/memories")
    assert m.warnings == [] and len(m.cards) > 200
    text = "\n".join(c.text for c in m.cards)
    # 歌曲卡是以小鈴的角度產生的，不能殘留沙耶香版的標記
    assert "小鈴有唱" in text and "沙耶香有唱" not in text and "沙耶香沒有唱" not in text
    assert m.search("小鈴 唱過 哪些歌")[0].title == "歌單：小鈴唱過的歌（總覽）"
    assert m.search("レディバグ")[0].title == "歌曲『レディバグ』"


def test_bundled_kozue_song_cards():
    from bot.memories import Memories

    m = Memories(ROOT / "characters/kozue/memories")
    assert m.warnings == [] and len(m.cards) > 200
    text = "\n".join(c.text for c in m.cards)
    # 歌曲卡是以梢的角度產生的，不能殘留其他角色版的標記
    assert "梢有唱" in text and "綴理有唱" not in text and "小鈴有唱" not in text
    assert m.search("梢 唱過 哪些歌")[0].title == "歌單：梢唱過的歌（總覽）"
    # 「水彩世界」另有 104 Ver.，原版要在前幾名
    hits = {c.title: c for c in m.search("水彩世界")[:3]}
    assert "歌曲『水彩世界』" in hits
    # 維基看不出來的關係：梢一個人作的曲、和花帆的第一個舞台
    assert "梢一個人作的曲" in hits["歌曲『水彩世界』"].text


def test_bundled_kosuzu_meets_cards():
    import re

    from bot.memories import Memories

    m = Memories(ROOT / "characters/kosuzu/memories")
    meets = [c for c in m.cards if re.match(r"\d{4}-\d{2}-\d{2}『", c.title)]
    assert len(meets) == 90
    text = "\n".join(c.text for c in meets)
    # 口頭禪統一寫成原作的「ちぇすとー」；聽錯的英文寫法不能留下（DOLLCHESTRA 除外）
    assert "ちぇすとー" in text and not re.search(r"(?<!DOLL)CHEST|Chest", text)
    assert m.search("おえかき 恐竜")[0].title == "2025-07-05『おえかきかちまちです！』"
    assert m.search("チェスト 聖誕")[0].title == "2025-12-25『クリスマスプレゼントのお届けです』"


def test_bundled_kozue_meets_cards():
    import re

    from bot.memories import Memories

    m = Memories(ROOT / "characters/kozue/memories")
    meets = [c for c in m.cards if re.match(r"\d{4}-\d{2}-\d{2}『", c.title)]
    assert len(meets) == 93
    text = "\n".join(c.text for c in meets)
    # 小隊名照原作寫；筆記裡的英文、音譯寫法與聽錯的曲名不能留下
    assert "スリーズブーケ" in text and not re.search(r"Cerise|斯里姿|斯莉姿|Mira-Cra|千変万花|幻妖夜行|Aurora Flare", text)
    assert m.search("蓮ノ大三角 沙知 取名")[0].title == "2025-03-06『見納めの会』"
    assert m.search("機械先生 關不掉")[0].title.startswith("20")
    assert m.search("音大 聲樂科")[0].title == "2025-03-27『乙宗 梢、最後のWith×MEETSです』"


def test_character_dir_moves_every_default(tmp_path):
    d = tmp_path / "kosuzu"
    d.mkdir()
    (d / "character.toml").write_text('display_name = "小鈴"\naliases = ["小鈴", "こすず"]\n', encoding="utf-8")
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.persona_path == d / "persona.md"
    assert c.replies_path == d / "replies.toml"
    assert c.lore_dir == d / "lore"
    assert c.memories_dir == d / "memories"
    assert c.bot_display_name == "小鈴"
    assert c.bot_aliases == ("小鈴", "こすず")
    # 同一個資料夾跑多個角色時，資料庫各自分開
    assert c.db_path == Path("data/kosuzu.sqlite3")


def test_env_overrides_character_file(tmp_path):
    (tmp_path / "character.toml").write_text('display_name = "小鈴"\naliases = ["小鈴"]\n', encoding="utf-8")
    c = Config.from_env({"CHARACTER_DIR": str(tmp_path), "BOT_DISPLAY_NAME": "徒町", "BOT_ALIASES": "a,b",
                         "PERSONA_PATH": "p.md", "LORE_DIR": "none", "DB_PATH": "x.sqlite3"})
    assert c.bot_display_name == "徒町" and c.bot_aliases == ("a", "b")
    assert c.persona_path == Path("p.md") and c.lore_dir is None and c.db_path == Path("x.sqlite3")


def test_missing_character_file_falls_back_to_folder_name(tmp_path):
    d = tmp_path / "kosuzu"
    d.mkdir()
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.bot_display_name == "kosuzu" and c.bot_aliases == ()


@pytest.mark.parametrize("content", ["display_name = ", 'aliases = "小鈴"', "display_name = 3"])
def test_bad_character_file_is_rejected(tmp_path, content):
    (tmp_path / "character.toml").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="character.toml"):
        Config.from_env({"CHARACTER_DIR": str(tmp_path)})


def test_bundled_kaho_loads():
    """花帆：lore 01–05、卡片語音、歌曲卡、With×MEETS 回憶卡（2026-11-14）。"""
    from bot.lore import Lore
    from bot.persona import Persona
    from bot.replies import Replies

    d = ROOT / "characters/kaho"
    c = Config.from_env({"CHARACTER_DIR": str(d)})
    assert c.bot_display_name == "花帆" and "日野下花帆" in c.bot_aliases
    assert "自稱「我」" in Persona(c.persona_path).text
    assert Replies(c.replies_path).warnings == []
    lore = Lore(c.lore_dir)
    assert lore.warnings == []
    assert lore.files == ["共通_01_社團與成員.md", "01_個人設定.md", "02_人物關係.md", "03_重要事件時間線.md",
                          "04_日常與小事.md", "05_With×MEETS.md"]
    assert 10_000 < len(lore.text) < 35_000  # 含共通 lore（約 2,400 字）
    for junk in ("〔", "台本", "百科", "raw_", ".txt", "間章", "中集", "Starsness", "劇場版", "scripts/", "OCR",
                 "待核對", "待補", "BD 特典", "入場者特典", "蓮ノート", "`"):
        assert junk not in lore.text, junk


def test_bundled_kaho_song_cards():
    from bot.memories import Memories

    m = Memories(ROOT / "characters/kaho/memories")
    assert m.warnings == [] and len(m.cards) > 200
    text = "\n".join(c.text for c in m.cards)
    # 歌曲卡是以花帆的角度產生的，不能殘留其他角色版的標記
    assert "花帆有唱" in text and "小鈴有唱" not in text and "沙耶香有唱" not in text
    assert m.search("花帆 唱過 哪些歌")[0].title == "歌單：花帆唱過的歌（總覽）"
    assert m.search("フォーチュンムービー")[0].title == "歌曲『フォーチュンムービー』"


def test_bundled_kaho_meets_cards():
    import re

    from bot.memories import Memories

    m = Memories(ROOT / "characters/kaho/memories")
    meets = [c for c in m.cards if re.match(r"\d{4}-\d{2}-\d{2}『", c.title)]
    assert len(meets) == 164
    text = "\n".join(c.text for c in meets)
    # 妹妹照原作寫「ふたば」「みのり」，瑠璃乃的姓寫「大沢」；聽錯的寫法只留在 aliases.txt
    assert "ふたば" in text and not re.search(r"雙葉|双葉|實里|美成|大澤|姫芽", text)
    assert m.search("最後のWith×MEETS")[0].title == "2026-03-28『日野下花帆、最後のWith×MEETSだよ！』"
    assert m.search("雙胞胎 妹妹 雙黃蛋")[0].title == "2024-04-08『入学1年記念雑談！』"
    assert m.search("さざなみちゃん 吉他")[0].title == "2026-01-19『かほとるりでおしゃべり配信！』"
