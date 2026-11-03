from bot.history import SUMMARY_PREFIX, build_prompt, merge_consecutive
from bot.storage import StoredMessage

CH = 500


def test_prompt_order_persona_summary_recent_current():
    recent = [
        StoredMessage(1, CH, "KO", "今天好累", False),
        StoredMessage(2, CH, "大小姐", "哼，誰叫你熬夜。", True),
        StoredMessage(3, CH, "Amy", "我也是", False),
    ]
    msgs = build_prompt("PERSONA", "大家昨天在聊考試", recent, "KO", "大小姐安慰我")
    assert msgs[0] == {"role": "system", "content": "PERSONA"}
    # 摘要放在 user 角色並標明是參考資料，後面緊接的 user 訊息會合併進來
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == f"{SUMMARY_PREFIX}<摘要>\n大家昨天在聊考試\n</摘要>\nKO: 今天好累"
    assert msgs[2] == {"role": "assistant", "content": "哼，誰叫你熬夜。"}
    # 相鄰的 user 訊息會合併，當前訊息在最後
    assert msgs[3] == {"role": "user", "content": "Amy: 我也是\nKO: 大小姐安慰我"}
    assert len(msgs) == 4


def test_prompt_without_summary_keeps_persona_first():
    msgs = build_prompt("PERSONA", None, [], "KO", "hi")
    assert msgs == [{"role": "system", "content": "PERSONA"}, {"role": "user", "content": "KO: hi"}]


def test_persona_prefix_stable_across_calls():
    a = build_prompt("PERSONA", "S", [], "KO", "1")
    b = build_prompt("PERSONA", "S", [StoredMessage(1, CH, "A", "x", False)], "KO", "2")
    assert a[0] == b[0]
    # 摘要區塊的文字也相同，前綴快取仍能涵蓋到摘要為止
    block = f"{SUMMARY_PREFIX}<摘要>\nS\n</摘要>\n"
    assert a[1]["content"].startswith(block) and b[1]["content"].startswith(block)


def test_merge_does_not_merge_system():
    msgs = [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}]
    assert merge_consecutive(msgs) == msgs


async def test_history_trim_and_summary_trigger(make_service, fake_openai, storage):
    svc = make_service(history_max_messages=6)
    hist = svc.history
    for i in range(6):
        hist.add(CH, "KO", f"msg{i}")
    assert not hist.needs_summary(CH)
    assert await hist.maybe_summarize(CH) is False
    assert fake_openai.completions.calls == []

    hist.add(CH, "KO", "msg6")
    assert hist.needs_summary(CH)
    fake_openai.completions.replies = ["摘要A"]
    assert await hist.maybe_summarize(CH) is True

    call = fake_openai.completions.calls[-1]
    assert call["model"] == "deepseek-flash"
    # 7 則壓到剩 3 則（上限的一半），最舊的 4 則進摘要
    assert "msg0" in call["messages"][1]["content"] and "msg3" in call["messages"][1]["content"]
    assert "msg4" not in call["messages"][1]["content"]
    assert storage.get_summary(CH) == "摘要A"
    assert [m.content for m in hist.recent(CH)] == ["msg4", "msg5", "msg6"]


async def test_summary_merges_previous_summary(make_service, fake_openai, storage):
    svc = make_service(history_max_messages=4)
    storage.set_summary(CH, "舊摘要")
    for i in range(5):
        svc.history.add(CH, "KO", f"m{i}")
    fake_openai.completions.replies = ["新摘要"]
    await svc.history.maybe_summarize(CH)
    assert "舊摘要" in fake_openai.completions.calls[-1]["messages"][1]["content"]
    assert storage.get_summary(CH) == "新摘要"


async def test_summary_failure_keeps_messages_until_hard_limit(make_service, fake_openai, storage):
    svc = make_service(history_max_messages=4)
    fake_openai.completions.default = RuntimeError("down")
    for i in range(7):
        svc.history.add(CH, "KO", f"m{i}")
    assert await svc.history.maybe_summarize(CH) is False
    assert storage.count_messages(CH) == 7  # 未超過兩倍上限，先保留
    for i in range(7, 10):
        svc.history.add(CH, "KO", f"m{i}")
    await svc.history.maybe_summarize(CH)
    assert storage.count_messages(CH) == 8  # 超過兩倍上限就丟掉最舊的
    assert storage.get_summary(CH) is None


async def test_recent_limited_to_max(make_service):
    svc = make_service(history_max_messages=3)
    for i in range(5):
        svc.history.add(CH, "KO", f"m{i}")
    assert [m.content for m in svc.history.recent(CH)] == ["m2", "m3", "m4"]


async def test_reset_clears_history_and_summary(make_service, storage):
    svc = make_service()
    svc.history.add(CH, "KO", "x")
    storage.set_summary(CH, "s")
    svc.history.reset(CH)
    assert storage.count_messages(CH) == 0 and storage.get_summary(CH) is None
