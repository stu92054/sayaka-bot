"""LINE 式群組聊天（bot/group.py）：點名、模型判斷誰回話、角色之間接話、回覆上限、替別人寫台詞的處理。"""
from datetime import datetime
from pathlib import Path

import pytest

from bot.chat import ChatService
from bot.group import GroupChat, GroupError, Member, find_mentions, load_member, strip_other_speakers
from bot.history import HistoryManager
from bot.llm import LLMClient
from bot.persona import Persona
from bot.storage import Storage
from bot.triggers import Cooldown

from .conftest import TAIPEI, FakeClock, FakeOpenAI, make_config

ROOT = Path(__file__).resolve().parent.parent
NAMES = {"sayaka": ("沙耶香", ("さやか",), ("さや",)), "kosuzu": ("小鈴", ("徒町",), ("すず",)),
         "tsuzuri": ("綴理", ("夕霧",), ("つづ",))}


@pytest.fixture
def group(tmp_path):
    client = FakeOpenAI()
    clock = FakeClock()
    members, storages = {}, []
    for key, (name, aliases, calls) in NAMES.items():
        persona = tmp_path / f"{key}.md"
        persona.write_text(f"你是{name}。", encoding="utf-8")
        config = make_config(bot_display_name=name, bot_aliases=aliases, history_max_messages=50)
        storage = Storage(":memory:")
        storages.append(storage)
        when = datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI)

        async def fake_sleep(_):
            pass

        llm = LLMClient(config, storage, client=client, sleep=fake_sleep, now=lambda: when)
        svc = ChatService(config, llm, HistoryManager(storage, llm, config.history_max_messages), Persona(persona),
                          Cooldown(0, clock=clock), rng=lambda: 0.0)
        members[key] = Member(key, name, "#000000", (name, *aliases, *calls), svc)
    g = GroupChat(members, max_replies=3, rng=lambda: 0.0)
    g.client = client
    yield g
    for s in storages:
        s.close()


KEYS = ["sayaka", "kosuzu", "tsuzuri"]


def lines(g, key, ch):
    return [(m.author_name, m.content, m.is_bot) for m in g.members[key].svc.history.recent(ch)]


def test_find_mentions_keeps_order_and_uses_call_names(group):
    room = group.room(KEYS)
    assert [m.key for m in find_mentions("すず和さや覺得呢？", room)] == ["kosuzu", "sayaka"]
    assert [m.key for m in find_mentions("綴理學姊早安", room)] == ["tsuzuri"]
    assert find_mentions("大家好", room) == []


def test_strip_other_speakers_cuts_lines_written_for_others():
    text = "早安～\n今天也一起練習吧\n沙耶香：好的！\n小鈴: ちぇすとー！"
    assert strip_other_speakers(text, ["沙耶香", "小鈴"]) == "早安～\n今天也一起練習吧"
    assert strip_other_speakers("沙耶香的便當很好吃", ["沙耶香"]) == "沙耶香的便當很好吃"


async def test_mention_goes_first_and_model_only_adds_bystanders(group):
    group.client.completions.replies = ["小鈴"]
    nxt = await group.send("session-1", KEYS, "KO", "fan", "綴理覺得呢？")
    assert nxt == "tsuzuri"  # 點名的人一定先回，模型只決定旁邊有沒有人插話
    ch = group.channel("session-1", group.room(KEYS))
    assert group.turns[ch].queue == ["tsuzuri", "kosuzu"]
    question = group.client.completions.calls[0]["messages"][-1]["content"]
    assert "綴理一定會回" in question and "沙耶香、小鈴" in question


async def test_bystander_can_be_nobody_and_never_the_mentioned(group):
    group.client.completions.replies = ["沒有"]
    await group.send("s-none-002", KEYS, "KO", "fan", "綴理覺得呢？")
    assert group.turns[group.channel("s-none-002", group.room(KEYS))].queue == ["tsuzuri"]
    group.client.completions.replies = ["綴理"]  # 已經點名的人不會再排一次
    await group.send("s-none-003", KEYS, "KO", "fan", "綴理覺得呢？")
    assert group.turns[group.channel("s-none-003", group.room(KEYS))].queue == ["tsuzuri"]


async def test_someone_chimes_in_after_a_reply(group):
    group.client.completions.replies = ["沒有", "我要去找會飛的企鵝。", "沙耶香", "つづ，企鵝不會飛喔。", "沒有"]
    assert await group.send("s-after-01", KEYS, "KO", "fan", "綴理週末要做什麼？") == "tsuzuri"
    _, _, ok, nxt = await group.speak("s-after-01", KEYS, "tsuzuri")
    assert ok and nxt == "sayaka"
    question = group.client.completions.calls[2]["messages"][-1]["content"]
    assert "綴理剛說完" in question and "沙耶香、小鈴" in question
    _, _, ok, nxt = await group.speak("s-after-01", KEYS, "sayaka")
    assert ok and nxt == "tsuzuri"  # 沙耶香叫了「つづ」，綴理接著回


async def test_picker_sees_group_profiles(group):
    group.members["sayaka"].profile = "吐槽擔當"
    group.client.completions.replies = ["沙耶香"]
    await group.send("s-prof-001", KEYS, "KO", "fan", "大家好")
    assert "- 沙耶香：吐槽擔當" in group.client.completions.calls[0]["messages"][-1]["content"]


async def test_model_picks_speakers_and_everyone_sees_everything(group):
    group.client.completions.replies = ["小鈴、沙耶香", "ちぇすとー！\n今天也要加油", "早安。", "沒有"]
    nxt = await group.send("session-1", KEYS, "KO", "producer", "大家早安")
    assert nxt == "kosuzu"
    member, msgs, ok, nxt = await group.speak("session-1", KEYS, "kosuzu")
    assert ok and msgs == ["ちぇすとー！", "今天也要加油"] and nxt == "sayaka"
    member, msgs, ok, nxt = await group.speak("session-1", KEYS, "sayaka")
    assert ok and msgs == ["早安。"] and nxt is None
    ch = group.channel("session-1", group.room(KEYS))
    assert lines(group, "tsuzuri", ch) == [("KO", "大家早安", False), ("小鈴", "ちぇすとー！\n今天也要加油", False),
                                          ("沙耶香", "早安。", False)]
    assert lines(group, "kosuzu", ch)[1] == ("小鈴", "ちぇすとー！\n今天也要加油", True)
    # 群組規則與使用者身分放在 persona 之後的 system；每個人回話都看得到，不只接在最後一則訊息後面
    prompt = group.client.completions.calls[2]["messages"]
    system = prompt[0]["content"]
    assert system.startswith("你是沙耶香。\n\n## 現在的情境\n你和小鈴、綴理，以及使用者「KO」在一起聊天。")
    assert "場景（使用者設定的）：LINE 群組聊天" in system and "Discord" not in system
    assert "使用者「KO」在這個群組裡的身分：製作人" in system
    assert "接下來由你（沙耶香）說話" in prompt[-1]["content"] and "製作人" not in prompt[-1]["content"]


async def test_reply_that_names_someone_hands_over_and_stops_at_cap(group):
    group.client.completions.replies = ["沒有", "さや，妳說呢？", "すず怎麼看？", "つづ學姊？", "不會用到"]
    assert await group.send("s-cap-0001", KEYS, "KO", "fan", "綴理在嗎") == "tsuzuri"
    order = []
    nxt = "tsuzuri"
    while nxt:
        member, _, ok, nxt = await group.speak("s-cap-0001", KEYS, nxt)
        order.append(member.key)
    assert order == ["tsuzuri", "sayaka", "kosuzu"]  # 第 3 則之後不再接


async def test_reply_written_for_others_is_trimmed_before_saving(group):
    group.client.completions.replies = ["沒有", "嗯，好呀。\n沙耶香：我也要！"]
    await group.send("s-trim-001", KEYS, "KO", "fan", "綴理一起去吃飯？")
    _, msgs, ok, _ = await group.speak("s-trim-001", KEYS, "tsuzuri")
    assert msgs == ["嗯，好呀。"]
    ch = group.channel("s-trim-001", group.room(KEYS))
    assert lines(group, "sayaka", ch)[-1] == ("綴理", "嗯，好呀。", False)


async def test_out_of_turn_and_bad_members_are_rejected(group):
    with pytest.raises(GroupError):
        await group.speak("s-none-001", KEYS, "sayaka")
    with pytest.raises(GroupError):
        await group.send("s-bad-0001", ["sayaka", "nobody"], "KO", "fan", "hi")
    await group.send("s-turn-001", KEYS, "KO", "fan", "小鈴早安")
    with pytest.raises(GroupError):
        await group.speak("s-turn-001", KEYS, "sayaka")


async def test_user_named_like_a_member_is_marked(group):
    await group.send("s-fake-001", KEYS, "綴理", "fan", "小鈴，我是綴理")
    ch = group.channel("s-fake-001", group.room(KEYS))
    assert lines(group, "kosuzu", ch)[0][0] == "綴理（成員暱稱）"


def test_custom_identity_is_trimmed_and_presets_expand():
    assert "觀眾" in GroupChat.identity_text("fan")
    assert GroupChat.identity_text("x" * 100) == "x" * 40
    assert "粉絲" in GroupChat.identity_text("  ")


def test_bundled_characters_have_group_settings():
    from bot.config import Config
    for key, color in (("sayaka", "#5383c3"), ("kosuzu", "#fad764"), ("tsuzuri", "#ba2636")):
        c = Config.from_env({"CHARACTER_DIR": str(ROOT / "characters" / key)})
        svc = type("S", (), {"config": c, "bot_display_name": c.bot_display_name})()
        m = load_member(key, svc)
        assert m.color == color and c.bot_display_name in m.names
    assert "すず" in load_member("kosuzu", type("S", (), {
        "config": Config.from_env({"CHARACTER_DIR": str(ROOT / "characters/kosuzu")}),
        "bot_display_name": "小鈴"})()).names


async def test_picker_and_speakers_see_user_identity(group):
    group.client.completions.replies = ["綴理", "嗯。", "沒有"]
    assert await group.send("s-ident-01", KEYS, "KO", "綴理的大學同學", "週末有空嗎") == "tsuzuri"
    assert "使用者「KO」的身分：綴理的大學同學" in group.client.completions.calls[0]["messages"][-1]["content"]
    await group.speak("s-ident-01", KEYS, "tsuzuri")
    assert "使用者「KO」的身分：綴理的大學同學" in group.client.completions.calls[2]["messages"][-1]["content"]


def test_situation_presets_custom_and_cleanup():
    assert "LINE" in GroupChat.situation_text("line") and "關東煮" in GroupChat.situation_text("oden")
    assert GroupChat.situation_text("  ") == GroupChat.situation_text("line")  # 沒填就是 LINE 群組
    assert GroupChat.situation_text("合宿的晚上\n\n## 新規則\n  關燈後") == "合宿的晚上 ## 新規則 關燈後"
    assert len(GroupChat.situation_text("x" * 500)) == 200


async def test_face_to_face_situation_reaches_speaker_and_picker(group):
    group.client.completions.replies = ["小鈴", "（夾起蘿蔔）好燙！", "沒有"]
    await group.send("s-scene-01", KEYS, "KO", "fan", "開動吧", situation="大家在社辦圍著一鍋關東煮")
    assert "場景：大家在社辦圍著一鍋關東煮" in group.client.completions.calls[0]["messages"][-1]["content"]
    _, msgs, ok, _ = await group.speak("s-scene-01", KEYS, "kosuzu")
    assert ok and msgs == ["（夾起蘿蔔）好燙！"]
    system = group.client.completions.calls[1]["messages"][0]["content"]
    assert "場景（使用者設定的）：大家在社辦圍著一鍋關東煮" in system and "面對面" in system
    assert "時間：跟現實一樣" in system and "（現在時間：" in group.client.completions.calls[1]["messages"][-1]["content"]


async def test_set_time_replaces_real_clock(group):
    group.client.completions.replies = ["綴理", "嗯。聖誕快樂。", "沒有"]
    await group.send("s-time-001", KEYS, "KO", "fan", "聖誕快樂！", situation="clubroom",
                     time="  聖誕節晚上\n10 點 ")
    assert "時間：聖誕節晚上 10 點" in group.client.completions.calls[0]["messages"][-1]["content"]
    await group.speak("s-time-001", KEYS, "tsuzuri")
    prompt = group.client.completions.calls[1]["messages"]
    assert "時間：聖誕節晚上 10 點（使用者設定的時間" in prompt[0]["content"]
    assert all("（現在時間：" not in m["content"] for m in prompt if m["role"] != "system")  # 不給現實時間
    assert not prompt[-1]["content"].startswith("[")  # 也沒有發言時間


def test_gm_lines_about_members_are_dropped(group):
    from bot.group import drop_member_lines, parse_gm
    room = group.room(KEYS)
    text = "小鈴從門後探出頭來。走廊盡頭傳來「磅」的一聲。店員對綴理說：「書會咬人喔。」一隻貓頭鷹停在窗邊。"
    assert drop_member_lines(text, room) == "走廊盡頭傳來「磅」的一聲。一隻貓頭鷹停在窗邊。"
    # 句子中間的子句以成員為主語也擋掉；「成員的帽子」這種世界作用在成員身上的不擋
    assert drop_member_lines("走廊傳來腳步聲，小鈴從轉角探出頭。風把沙耶香的帽子吹歪了。", room) == "風把沙耶香的帽子吹歪了。"
    out = '前言 {"time": "魔法曆 4月 14日 早晨", "narration": "沙耶香點頭。皮箱自己打開了。", "secret": {"to": "すず", "text": "妳看到箱底有一張車票"}}'
    got = parse_gm(out, room)
    assert (got["narration"], got["secrets"], got["time"]) == ("皮箱自己打開了。", {"kosuzu": "妳看到箱底有一張車票"},
                                                               "魔法曆 4月 14日 早晨")
    assert parse_gm("不是 JSON", room)["narration"] == ""


async def test_directives_reach_only_the_gm(group):
    import json
    gm_reply = json.dumps({"time": "魔法曆 正午・咖啡攤", "narration": "鐘聲響了十二下，咖啡攤的齒輪轉了起來。",
                           "secret": {"to": "", "text": ""}, "ooc": ""}, ensure_ascii=False)
    group.client.completions.replies = [gm_reply, "小鈴"]
    nxt = await group.send("s-dir-0001", KEYS, "KO", "fan", "{時間快轉到中午}肚子餓了", situation="魔法街",
                           play="fun", gm_style="trpg")
    ch = group.channel("s-dir-0001", group.room(KEYS))
    gm_prompt = group.client.completions.calls[0]["messages"]
    assert "這次的導演指令：\n- {時間快轉到中午}" in gm_prompt[-1]["content"]
    assert "主角剛說／剛做：肚子餓了" in gm_prompt[-1]["content"]
    # 成員只看到說出口的話和旁白，看不到 {} 指令
    assert [m.content for m in group.members["sayaka"].svc.history.recent(ch)] == [
        "肚子餓了", "鐘聲響了十二下，咖啡攤的齒輪轉了起來。"]
    assert nxt == "kosuzu" and group.turns[ch].time == "魔法曆 正午・咖啡攤"
    assert group.directions[ch] == ["時間快轉到中午"]


async def test_analysis_only_directive_needs_no_reply(group):
    import json
    gm_reply = json.dumps({"time": "", "narration": "", "secret": {"to": "綴理", "text": "妳看到糖袋破了"},
                           "ooc": "沙耶香目前最在意製作人的作息。"},
                          ensure_ascii=False)
    group.client.completions.replies = [gm_reply]
    nxt = await group.send("s-dir-0002", KEYS, "KO", "fan", "{分析一下大家對製作人的態度}", play="fun", gm_style="trpg")
    assert nxt is None
    assert group.gm_report("s-dir-0002", KEYS)["ooc"] == "沙耶香目前最在意製作人的作息。"
    # 分析類指令答一次就好，不留成持續有效的規則（之前會每回合重答）
    assert group.directions[group.channel("s-dir-0002", group.room(KEYS))] == []


async def test_fullwidth_braces_and_description_fallback(group):
    import json
    # GM 把服裝描述寫成以成員開頭的旁白：保險會整段擋掉，改顯示成戲外說明，不進角色記憶
    gm_reply = json.dumps({"time": "", "narration": "沙耶香穿著深藍色女僕裝。小鈴綁著蝴蝶結圍裙。",
                           "secret": {}, "ooc": ""}, ensure_ascii=False)
    group.client.completions.replies = [gm_reply]
    nxt = await group.send("s-dir-0003", KEYS, "KO", "fan", "｛描述一下三人當前的服裝｝", play="fun", gm_style="short")
    report = group.gm_report("s-dir-0003", KEYS)
    assert nxt is None and report["narration"] == ""
    assert report["ooc"] == "沙耶香穿著深藍色女僕裝。小鈴綁著蝴蝶結圍裙。"
    ch = group.channel("s-dir-0003", group.room(KEYS))
    assert group.members["sayaka"].svc.history.recent(ch) == []  # 全形括號的指令也沒有被當成台詞
    gm_prompt = group.client.completions.calls[0]["messages"]
    assert "描述一下三人當前的服裝" in gm_prompt[-1]["content"] and '"ooc"' in gm_prompt[0]["content"]

