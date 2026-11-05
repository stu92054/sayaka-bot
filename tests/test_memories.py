"""回憶卡（memories/）載入、BM25 檢索與 recall 工具。"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from bot.commands import reload_persona_and_replies
from bot.config import Config
from bot.llm import LLMClient
from bot.main import PersonaBot
from bot.memories import (KEYWORDS_PREFIX, RECALL_GUIDE, RECALL_RESULT_NOTE, Memories, build_recall_tool,
                          parse_cards, tokenize)
from bot.persona import Persona
from bot.tools import WEB_TOOLS_GUIDE, ToolRunner, build_web_tools

from .conftest import BOT_ID, TAIPEI, make_config, make_message
from .test_tools import FakeWebAPI, make_web

ROOT = Path(__file__).resolve().parent.parent

CARDS = """# 回憶卡

> 說明文字，不是卡片。

## 2023-08-16『電台 第３回』
103 期｜出場：A、綴理｜電台
- 第一位旅人是綴理，綴理最後睡著了。
關鍵詞：旅人、ゲスト、蘆筍

## 2024-10-10『突然ですがクイズです』
104 期｜出場：綴理、A
- 在金澤城公園幫迷路的小女孩找奶奶，被叫媽媽。
關鍵詞：さやママ、ママ

## 2025-12-11『ASMR』
105 期｜出場：A
- 第一次挑戰 ASMR，演出叫人起床。
關鍵詞：ASMR、起床
"""


@pytest.fixture
def mem_dir(tmp_path):
    d = tmp_path / "memories"
    d.mkdir()
    (d / "cards.md").write_text(CARDS, encoding="utf-8")
    (d / "aliases.txt").write_text("# 註解\n綴理 つづり 夕霧\n媽媽 ママ\n", encoding="utf-8")
    return d


# --- 斷詞與解析 ---

def test_tokenize_cjk_bigrams_and_numbers():
    tokens = tokenize("綴理先輩 2024-01-13 ASMR さやママ")
    assert {"綴", "理", "綴理", "先輩"} <= set(tokens)
    assert {"2024", "1", "13", "asmr"} <= set(tokens)  # 數字去掉前導 0、英文轉小寫
    assert "さや" in tokens and "さ" not in tokens  # 假名只取相鄰兩字


def test_parse_cards_skips_header_and_hides_keywords():
    cards = parse_cards(CARDS)
    assert [c.title[:10] for c in cards] == ["2023-08-16", "2024-10-10", "2025-12-11"]
    assert all(KEYWORDS_PREFIX not in c.text for c in cards)
    assert cards[0].tokens["蘆筍"] == 1  # 關鍵詞仍可檢索


# --- 檢索 ---

def test_search_ranks_by_keyword(mem_dir):
    m = Memories(mem_dir)
    assert m.enabled and len(m.cards) == 3 and m.files == ["cards.md"]
    assert m.search("ASMR")[0].title.startswith("2025-12-11")
    assert m.search("旅人 綴理")[0].title.startswith("2023-08-16")
    assert m.search("完全無關的東西 xyz") == []


def test_aliases_expand_with_lower_weight(mem_dir):
    m = Memories(mem_dir)
    assert m.search("つづり 旅人")[0].title.startswith("2023-08-16")  # 日文名透過別名找到
    terms = m.query_terms("ママ")
    assert terms["ママ"] == 1.0 and terms["媽媽"] == 0.5


def test_question_words_are_ignored(mem_dir):
    m = Memories(mem_dir)
    assert "妳" not in m.query_terms("妳那次被叫媽媽是怎樣？")
    assert m.search("妳那次被叫媽媽是怎樣？")[0].title.startswith("2024-10-10")


def test_results_limit(mem_dir):
    assert len(Memories(mem_dir, results=1).search("綴理")) == 1


def test_missing_dir_disables(tmp_path):
    m = Memories(tmp_path / "nope")
    assert not m.enabled and any("找不到" in w for w in m.warnings)


def test_reload_keeps_old_cards_on_failure(mem_dir):
    m = Memories(mem_dir)
    (mem_dir / "cards.md").write_text("沒有卡片", encoding="utf-8")
    warnings = m.reload()
    assert m.enabled and len(m.cards) == 3 and "保留目前的回憶卡" in warnings


# --- recall 工具 ---

async def test_recall_tool_returns_cards_and_skips_quota(mem_dir):
    used = []
    runner = ToolRunner([build_recall_tool(Memories(mem_dir))], has_quota=lambda: False, on_use=used.append)
    out = await runner.run("recall", json.dumps({"query": "ASMR"}, ensure_ascii=False))
    assert out.startswith(RECALL_RESULT_NOTE) and "第一次挑戰 ASMR" in out and KEYWORDS_PREFIX not in out
    assert used == []  # 不算每日查詢額度（額度用完也能回想）
    assert "找不到" in await runner.run("recall", json.dumps({"query": "xyz"}))
    assert "關鍵字" in await runner.run("recall", "{}")


def test_guide_is_character_neutral():
    assert "recall" in RECALL_GUIDE and "不要編造" in RECALL_GUIDE
    for name in ("沙耶香", "さやか", "綴理"):
        assert name not in RECALL_GUIDE


# --- 與聊天流程整合 ---

async def test_recall_works_without_web_tools(make_service, fake_openai, mem_dir):
    svc = make_service(web_tools_enabled=False)
    svc.memories = Memories(mem_dir)
    fake_openai.completions.replies = [{"tool_calls": [("recall", {"query": "ASMR"})]}, "那次是叫大家起床喔。"]
    reply = await svc.handle_message(make_message("妳 ASMR 那次在做什麼？", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == ["那次是叫大家起床喔。"]
    first, second = fake_openai.completions.calls
    assert [t["function"]["name"] for t in first["tools"]] == ["recall"]
    assert RECALL_GUIDE in [m["content"] for m in first["messages"]]
    assert "第一次挑戰 ASMR" in second["messages"][-1]["content"]


async def test_recall_and_web_tools_together(make_service, fake_openai, mem_dir):
    svc = make_service(web_tools_enabled=True)
    svc.memories = Memories(mem_dir)
    svc.web_tools = build_web_tools(make_web(FakeWebAPI()))
    await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    call = fake_openai.completions.calls[0]
    assert [t["function"]["name"] for t in call["tools"]] == ["recall", "web_search", "fetch_page"]
    assert f"{RECALL_GUIDE}\n\n{WEB_TOOLS_GUIDE}" in [m["content"] for m in call["messages"]]


async def test_no_cards_means_no_tools(make_service, fake_openai, tmp_path):
    svc = make_service()
    svc.memories = Memories(tmp_path / "nope")
    await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert "tools" not in fake_openai.completions.calls[0]


# --- 設定、/persona_reload、隨附的回憶卡 ---

def test_config_memories():
    cfg = Config.from_env({})
    assert cfg.memories_dir == Path("memories") and cfg.recall_results == 3
    assert Config.from_env({"MEMORIES_DIR": "none"}).memories_dir is None
    assert any("RECALL_RESULTS" in p for p in Config.from_env({"RECALL_RESULTS": "0"}).validate())


async def test_persona_reload_reloads_memories(storage, fake_openai, persona_file, mem_dir):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file), memories=Memories(mem_dir))
    try:
        (mem_dir / "more.md").write_text("## 2026-01-01『新卡』\n- 新的回憶", encoding="utf-8")
        assert "已重新載入回憶卡（4 張）" in reload_persona_and_replies(bot)
        assert bot.chat.memories is bot.memories and len(bot.chat.memories.cards) == 4
    finally:
        await bot.close()


def test_shipped_memories_load():
    m = Memories(ROOT / "memories")
    assert m.warnings == [] and len(m.cards) >= 100 and m.aliases
    assert all(c.title[:4].isdigit() for c in m.cards)
    assert m.search("さやママ")[0].title.startswith("2024-10-10")


# --- 模型把呼叫寫成文字時，改為實際呼叫 ---

@pytest.mark.parametrize("text", ["[recall: ASMR]", '[recall query="ASMR"]', "recall(ASMR)", " [recall：ASMR] "])
def test_parse_text_call(mem_dir, text):
    runner = ToolRunner([build_recall_tool(Memories(mem_dir))])
    assert runner.parse_text_call(text) == ("recall", '{"query": "ASMR"}')


@pytest.mark.parametrize("text", ["好喔，[recall: ASMR] 我想想", "[web_search: x]", "那次啊……", ""])
def test_parse_text_call_ignores_normal_replies(mem_dir, text):
    runner = ToolRunner([build_recall_tool(Memories(mem_dir))])
    assert runner.parse_text_call(text) is None


async def test_text_call_is_executed(make_service, fake_openai, mem_dir):
    svc = make_service()
    svc.memories = Memories(mem_dir)
    fake_openai.completions.replies = ["[recall: ASMR]", "那次是叫大家起床喔。"]
    reply = await svc.handle_message(make_message("ASMR 那次？", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == ["那次是叫大家起床喔。"]
    second = fake_openai.completions.calls[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "recall"
    assert "第一次挑戰 ASMR" in second[-1]["content"]


async def test_text_call_on_final_round_is_not_sent(make_service, fake_openai, mem_dir):
    svc = make_service(web_max_tool_calls_per_reply=1)
    svc.memories = Memories(mem_dir)
    fake_openai.completions.replies = [{"tool_calls": [("recall", {"query": "ASMR"})]}, "[recall: 更多]"]
    reply = await svc.handle_message(make_message("ASMR 那次？", mentions=[BOT_ID]), BOT_ID)
    assert "[recall" not in "".join(reply.chunks)
