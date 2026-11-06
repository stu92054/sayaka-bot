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
