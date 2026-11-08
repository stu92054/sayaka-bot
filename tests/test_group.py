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
    assert system.startswith("你是沙耶香。\n\n## 現在的情境\n你在一個 LINE 群組") and "Discord" not in system
    assert "還有小鈴、綴理，以及使用者「KO」" in system
    assert "使用者「KO」在這個群組裡的身分：製作人" in system
    assert "輪到你（沙耶香）" in prompt[-1]["content"] and "製作人" not in prompt[-1]["content"]


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
