"""時間感知：附上現在時間、對話紀錄加上發言時間、人設說明。"""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot.config import Config
from bot.history import build_prompt, format_now, format_stamp, last_nudge, strip_leading_stamp
from bot.llm import LLMClient
from bot.main import PersonaBot
from bot.persona import Persona
from bot.storage import StoredMessage

from .conftest import BOT_ID, TAIPEI, make_config, make_message

ROOT = Path(__file__).resolve().parent.parent
CH = 500
NOW = datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI)  # 與 conftest 的 make_service 相同（週二）


def ts(*args) -> float:
    return datetime(*args, tzinfo=TAIPEI).timestamp()


# --- 格式 ---

def test_format_now():
    assert format_now(NOW) == "（現在時間：2026-11-03（週二）12:30，台灣時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）"
    assert format_now(datetime(2026, 11, 8, 23, 5, tzinfo=TAIPEI)) == "（現在時間：2026-11-08（週日）23:05，台灣時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）"
    tokyo = datetime(2026, 11, 3, 13, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert format_now(tokyo).endswith("13:30，日本時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）")
    paris = datetime(2026, 11, 3, 5, 30, tzinfo=ZoneInfo("Europe/Paris"))
    assert format_now(paris).endswith("05:30，Europe/Paris。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）")


def test_format_stamp_uses_bot_timezone():
    # 資料庫存的是 Unix 時間戳；UTC 2026-11-02 16:05 = 台北 11/03 00:05
    utc = datetime(2026, 11, 2, 16, 5, tzinfo=timezone.utc).timestamp()
    assert format_stamp(utc, TAIPEI) == "11/03 00:05"


def test_strip_leading_stamp():
    assert strip_leading_stamp("[11/03 12:31] 早安。") == "早安。"
    assert strip_leading_stamp("  [1/3 9:05]  早安") == "早安"
    assert strip_leading_stamp("早安 [11/03 12:31]") == "早安 [11/03 12:31]"  # 只處理開頭
    assert strip_leading_stamp("[注意] 這不是時間") == "[注意] 這不是時間"


# --- prompt 組裝 ---

RECENT = [
    StoredMessage(1, CH, "KO", "今天好累", False, ts(2026, 11, 2, 23, 50)),
    StoredMessage(2, CH, "沙耶香", "請早點休息。", True, ts(2026, 11, 2, 23, 51)),
    StoredMessage(3, CH, "Amy", "早安", False, ts(2026, 11, 3, 8, 0)),
]


def test_prompt_with_time_and_timestamps():
    msgs = build_prompt("PERSONA", None, RECENT, "KO", "現在幾點？", now=NOW, timestamps=True)
    assert msgs[1] == {"role": "user", "content": "[11/02 23:50] KO: 今天好累"}
    assert msgs[2] == {"role": "assistant", "content": "請早點休息。"}  # bot 自己的回覆不加時間
    assert msgs[3]["content"] == (
        "[11/03 08:00] Amy: 早安\n"
        "（現在時間：2026-11-03（週二）12:30，台灣時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）\n"
        "[11/03 12:30] KO: 現在幾點？"
    )


def test_prompt_time_without_timestamps():
    msgs = build_prompt("PERSONA", None, RECENT, "KO", "hi", now=NOW, timestamps=False)
    assert msgs[1]["content"] == "KO: 今天好累"
    assert msgs[-1]["content"] == "Amy: 早安\n（現在時間：2026-11-03（週二）12:30，台灣時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）\nKO: hi"


def test_prompt_without_now_unchanged():
    assert build_prompt("PERSONA", None, RECENT, "KO", "hi") == \
        build_prompt("PERSONA", None, RECENT, "KO", "hi", now=None, timestamps=True)


def test_history_without_created_at_has_no_stamp():
    old = [StoredMessage(1, CH, "KO", "舊訊息", False)]
    msgs = build_prompt("PERSONA", None, old, "KO", "hi", now=NOW, timestamps=True)
    assert msgs[1]["content"].startswith("KO: 舊訊息\n")


LATE = datetime(2026, 11, 4, 2, 30, tzinfo=TAIPEI)


def test_reminds_not_to_nudge_again_within_an_hour():
    recent = [
        StoredMessage(1, CH, "KO", "剛加班回來", False, ts(2026, 11, 4, 2, 20)),
        StoredMessage(2, CH, "沙耶香", "辛苦了。吃點熱的就早點休息吧。", True, ts(2026, 11, 4, 2, 21)),
        StoredMessage(3, CH, "KO", "吃了泡麵", False, ts(2026, 11, 4, 2, 25)),
        StoredMessage(4, CH, "沙耶香", "泡麵也好，下次加顆蛋吧。", True, ts(2026, 11, 4, 2, 26)),
    ]
    assert last_nudge(recent, LATE) == datetime(2026, 11, 4, 2, 21, tzinfo=TAIPEI)
    last = build_prompt("PERSONA", None, recent, "KO", "近江町市場有什麼推薦？", now=LATE, timestamps=True)[-1]["content"]
    assert "。你在 02:21 已經勸過休息，這次不要再提睡覺或時間，除非對方自己提起）" in last


def test_no_nudge_reminder_when_old_or_absent():
    old = [StoredMessage(1, CH, "沙耶香", "早點休息喔。", True, ts(2026, 11, 4, 1, 29))]  # 超過一小時
    plain = [StoredMessage(1, CH, "沙耶香", "近江町市場的海鮮丼很推薦。", True, ts(2026, 11, 4, 2, 21))]
    member = [StoredMessage(1, CH, "KO", "我要去睡了", False, ts(2026, 11, 4, 2, 21))]  # 成員自己說的不算
    no_time = [StoredMessage(1, CH, "沙耶香", "早點休息喔。", True)]
    for recent in (old, plain, member, no_time, []):
        assert last_nudge(recent, LATE) is None
        assert "已經勸過" not in build_prompt("P", None, recent, "KO", "hi", now=LATE)[-1]["content"]


def test_time_does_not_break_prefix_cache():
    """現在時間只出現在最後一則；固定的開頭與已存訊息的格式不隨時間改變。"""
    later = datetime(2026, 11, 4, 21, 0, tzinfo=TAIPEI)
    a = build_prompt("PERSONA", "摘要", RECENT, "KO", "1", lore="LORE", now=NOW, timestamps=True)
    b = build_prompt("PERSONA", "摘要", RECENT, "KO", "1", lore="LORE", now=later, timestamps=True)
    assert a[:-1] == b[:-1]
    assert a[-1]["content"].split("（現在時間")[0] == b[-1]["content"].split("（現在時間")[0]
    assert all("現在時間" not in m["content"] for m in a[:-1])


# --- 端對端 ---

async def test_chat_sends_current_time(make_service, fake_openai):
    svc = make_service()
    svc.history.timestamps = True
    await svc.handle_message(make_message("今天星期幾？", mentions=[BOT_ID]), BOT_ID)
    last = fake_openai.completions.calls[0]["messages"][-1]["content"]
    assert last == "（現在時間：2026-11-03（週二）12:30，台灣時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）\n[11/03 12:30] KO: 今天星期幾？"


async def test_deep_sends_current_time(make_service, fake_openai):
    svc = make_service()
    await svc.deep(CH, "KO", "今天幾號？")
    assert "（現在時間：2026-11-03（週二）12:30，台灣時間。只供參考：被問到才說時間，深夜勸休息時不要說出幾點）" in fake_openai.completions.calls[0]["messages"][-1]["content"]


async def test_show_current_time_off(make_service, fake_openai):
    svc = make_service(show_current_time=False)
    await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert all("現在時間" not in m["content"] for m in fake_openai.completions.calls[0]["messages"])


async def test_history_lines_get_stamped(make_service, fake_openai, storage):
    svc = make_service()
    svc.history.timestamps = True
    await svc.handle_message(make_message("今天好累", author_name="Amy"), BOT_ID)  # 不觸發，只記錄
    await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    last = fake_openai.completions.calls[0]["messages"][-1]["content"]
    stored = storage.recent_messages(CH, 10)[0]
    assert stored.created_at is not None
    assert last.startswith(f"[{format_stamp(stored.created_at, TAIPEI)}] Amy: 今天好累\n")


async def test_reply_leading_stamp_removed(make_service, fake_openai, storage):
    svc = make_service()
    fake_openai.completions.default = "[11/03 12:31] 現在是中午十二點半喔。"
    reply = await svc.handle_message(make_message("現在幾點？", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == ["現在是中午十二點半喔。"]
    assert storage.recent_messages(CH, 10)[-1].content == "現在是中午十二點半喔。"


async def test_reply_that_is_only_a_stamp_counts_as_empty(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = "[11/03 12:31]"
    reply = await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == [svc.replies["error"]] and not reply.recorded


async def test_summary_transcript_has_dates(make_service, fake_openai, storage):
    svc = make_service(history_max_messages=2)
    svc.history.timestamps = True
    for i in range(3):
        svc.history.add(CH, "KO", f"m{i}")
    fake_openai.completions.replies = ["摘要"]
    await svc.history.maybe_summarize(CH)
    call = fake_openai.completions.calls[-1]["messages"]
    assert "保留日期" in call[0]["content"]
    first = call[1]["content"].split("新的對話：\n")[1].splitlines()[0]
    assert first.startswith("[") and first.endswith("] KO: m0")


# --- 設定與人設 ---

def test_config_flags():
    cfg = Config.from_env({})
    assert cfg.show_current_time and cfg.history_timestamps
    cfg = Config.from_env({"SHOW_CURRENT_TIME": "false", "HISTORY_TIMESTAMPS": "0"})
    assert not cfg.show_current_time and not cfg.history_timestamps


@pytest.mark.parametrize("flag", [True, False])
async def test_bot_passes_history_timestamps(storage, fake_openai, persona_file, flag):
    config = make_config(history_timestamps=flag)
    bot = PersonaBot(config, storage, LLMClient(config, storage, client=fake_openai), Persona(persona_file))
    try:
        assert bot.history.timestamps is flag
    finally:
        await bot.close()


def test_llm_now_uses_bot_timezone(storage, fake_openai):
    llm = LLMClient(make_config(timezone="Asia/Tokyo"), storage, client=fake_openai)
    assert getattr(llm.now().tzinfo, "key", None) == "Asia/Tokyo"
    assert "日本時間" in format_now(llm.now())


def test_persona_explains_time():
    text = (ROOT / "persona.md").read_text(encoding="utf-8")
    assert "（現在時間：…）" in text and "[11/03 21:30]" in text
    assert "不要主動報時" in text
    assert "引號或時間" in text
