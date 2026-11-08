"""群組聊天「誰回話」的判斷評測：同一批標註好的對話片段，比較不同判斷方式。

用法（在專案根目錄）：python -m evals.group_pick.run <判斷方式> [cases.jsonl]
判斷方式：deepseek（目前 bot/group.py 用的方式）。決策模型（Clef、Jev、pplx-decider…）拿到金鑰後再加。

標註（cases.jsonl 每行一題）：
- members：群組成員；history：[說話者, 內容]，說話者是成員 key 或 "user"（使用者，暱稱 KO）
- label：每位成員接下來會不會回 yes／maybe／no
- first：可以接受的「第一個回話的人」；空陣列＝應該沒人回（等使用者）
評分：
- 第一位：判斷出的第一個人在 first 裡（first 是空的時，要判斷成沒人回）
- 排序錯：label 是 yes 的人，分數比 label 是 no 的人還低（每一對算一次）
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CASES = Path(__file__).with_name("cases.jsonl")


@dataclass
class Verdict:
    scores: dict[str, float]  # 每位成員「會接著回」的分數（0～1）
    first: str | None  # 判斷的第一個回話的人；None＝沒人回
    seconds: float


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gemini_key() -> None:
    """Gemini 金鑰：環境變數沒有的話，讀 AI_Story_Engine/.env 的 GEMINI_API_KEY（轉錄直播時用的同一把）。"""
    import os

    if os.environ.get("GEMINI_API_KEY"):
        return
    env = Path("D:/code/AI_Story_Engine/.env")
    for line in env.read_text(encoding="utf-8").splitlines() if env.is_file() else []:
        if line.startswith("GEMINI_API_KEY="):
            os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1].strip().strip('"')
            return
    raise SystemExit("找不到 GEMINI_API_KEY")


def build_group(backend: str = "deepseek"):
    """用真的角色資料夾（名字、別名、call_names、group_profile）組出群組，只拿來判斷，不產生回覆。"""
    import os

    from bot.webchat import build_service
    from bot.group import GroupChat, load_member

    keys = ["sayaka", "kosuzu", "tsuzuri"]
    os.chdir(ROOT)
    members = {}
    for k in keys:
        svc, _ = build_service(k, backend)
        members[k] = load_member(k, svc)
    return GroupChat(members)


async def judge_deepseek(group, case: dict) -> Verdict:
    from bot.group import find_mentions

    room = group.room(case["members"])
    channel = 10_000_000 + abs(hash(case["id"])) % 1_000_000
    svc = room[0].svc
    svc.history.reset(channel)
    for who, text in case["history"]:
        author = "KO" if who == "user" else group.members[who].name
        svc.history.add(channel, author, text, is_bot=(who == room[0].key))
    from bot.group import PICK_AFTER, PICK_EXTRA, PICK_OPEN

    t0 = time.monotonic()
    who, text = case["history"][-1]
    if who == "user":  # 使用者說話：點名的人先回，再問旁邊有沒有人插話；沒點名就問模型
        picked = find_mentions(text, room)
        if not picked:
            picked = await group._pick(room, channel, PICK_OPEN, limit=2)
        elif len(picked) < len(room):
            rest = [m for m in room if m not in picked]
            picked += await group._pick(room, channel, PICK_EXTRA.format(
                must="、".join(m.name for m in picked), cands="、".join(m.name for m in rest)), rest, limit=1)
    else:  # 角色剛說完：她點名的人先接；沒點名就問有沒有人想接一句（可以沒有）
        others = [m for m in room if m.key != who]
        picked = find_mentions(text, others)
        if not picked and others:
            picked = await group._pick(room, channel, PICK_AFTER.format(
                me=group.members[who].name, cands="、".join(m.name for m in others)), others, limit=1)
    svc.history.reset(channel)
    scores = {m.key: 0.0 for m in room}
    for i, m in enumerate(picked):
        scores[m.key] = 1.0 - 0.1 * i
    return Verdict(scores, picked[0].key if picked else None, time.monotonic() - t0)


CF_ENV = Path("D:/code/claudeflare/clef.env")
NOBODY = "nobody"


def cf_credentials() -> tuple[str, str]:
    """Cloudflare 帳號 ID 與 token：環境變數 CF_ACCOUNT_ID／CF_API_TOKEN，沒有就讀 CF_ENV（account_id=、token=）。"""
    import os
    import re

    account, token = os.environ.get("CF_ACCOUNT_ID", ""), os.environ.get("CF_API_TOKEN", "")
    if (not account or not token) and CF_ENV.is_file():
        found = dict(re.findall(r"^\s*(\w+)\s*=\s*['\"]?([^'\"\r\n]*)", CF_ENV.read_text(encoding="utf-8"), re.M))
        account, token = account or found.get("account_id", "").strip(), token or found.get("token", "").strip()
    if not account or not token:
        raise SystemExit("缺少 Cloudflare 帳號 ID 或 token")
    return account, token


def member_profile(m) -> str:
    """環境變數 PROFILE 決定給決策模型看多少角色資料：
    short（預設，character.toml 的 group_profile）、none、core（persona.md 的她是誰／說話方式／情緒與反應）、full（persona.md 全文）。"""
    import os
    import re

    mode = os.environ.get("PROFILE", "short")
    if mode == "none":
        return ""
    if mode == "short":
        return m.profile
    base = m.svc.persona.base
    if mode == "full":
        return base
    keep = re.findall(r"## (?:她是誰|說話方式（最重要）|情緒與反應)\n.*?(?=\n## |\Z)", base, re.S)
    return "\n\n".join(keep)


def clef_request(case: dict, group, model: str) -> dict:
    """一題一次呼叫：每位成員一個 noul（會不會接著說話），再加一個 choice（誰先說，或沒人）。"""
    room = [group.members[k] for k in case["members"]]
    talk = [f"{'KO' if who == 'user' else group.members[who].name}: {text}" for who, text in case["history"]]
    last = case["history"][-1][0]
    after = "使用者 KO 剛說完" if last == "user" else f"{group.members[last].name}剛說完"
    state = {
        "情境": "LINE 群組聊天。成員是幾位學園偶像，加上使用者 KO（粉絲）。真實的群組不是一對一："
                "有人在聊時，其他人常會插一句附和、吐槽或關心；但也不是每句話每個人都要回。",
        "成員": {m.name: member_profile(m) for m in room},
        "對話": talk,
    }
    questions = {m.key: {"type": "noul", "instructions": f"{after}。接下來{m.name}會在群組裡說話嗎（回應、插話都算）？"}
                 for m in room}
    criteria = {m.key: f"{m.name}" for m in room}
    criteria[NOBODY] = "沒有人：最後一則是在問使用者、或話題已經收尾，應該等使用者說話"
    questions["first"] = {"type": "choice", "criteria": criteria,
                          "instructions": f"{after}。接下來誰最應該第一個說話？使用者在問或在跟誰說話，就是那個人；"
                                          "只是被提到、沒被問的人不算。"}
    return {"model": model, "state": state, "questions": questions}


def make_clef_judge(model: str):
    async def judge(group, case: dict) -> Verdict:
        import urllib.request

        account, token = cf_credentials()
        url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
        body = json.dumps(clef_request(case, group, model), ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}",
                                                              "Content-Type": "application/json"})
        t0 = time.monotonic()
        out = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(req, timeout=60).read()))
        seconds = time.monotonic() - t0
        answers = out["result"]["answers"]
        scores = {k: float(answers[k]["noul"]) for k in case["members"]}
        first = answers["first"]["choice"]
        return Verdict(scores, None if first == NOBODY else first, seconds)

    return judge


def make_hybrid_judge(model: str, threshold: float = 0.5):
    """和 bot/group.py 同樣的流程，只把 DeepSeek 換成決策模型：
    使用者說完 → 點名的人先回，沒點名就選 noul 最高的人（一定有人回）；
    角色說完 → 她點名的人接，沒點名就看其他人的 noul，超過門檻才接。"""
    raw = make_clef_judge(model)

    async def judge(group, case: dict) -> Verdict:
        from bot.group import find_mentions

        v = await raw(group, case)
        room = [group.members[k] for k in case["members"]]
        who, text = case["history"][-1]
        if who == "user":
            named = find_mentions(text, room)
            first = named[0].key if named else max(case["members"], key=lambda k: v.scores[k])
        else:
            named = find_mentions(text, [m for m in room if m.key != who])
            others = [k for k in case["members"] if k != who]
            best = max(others, key=lambda k: v.scores[k]) if others else None
            first = named[0].key if named else (best if best and v.scores[best] >= threshold else None)
        scores = dict(v.scores)
        for i, m in enumerate(named):
            scores[m.key] = 2.0 - 0.1 * i  # 點名的人排在最前面
        return Verdict(scores, first, v.seconds)

    return judge


JUDGES = {"deepseek": judge_deepseek, "gemini": judge_deepseek, "clef-flash": make_clef_judge("clef-flash"), "clef": make_clef_judge("clef"),
          "clef-flash+rules": make_hybrid_judge("clef-flash"), "clef+rules": make_hybrid_judge("clef")}


def grade(case: dict, v: Verdict) -> tuple[bool, int]:
    first_ok = (v.first is None) if not case["first"] else (v.first in case["first"])
    yes = [k for k, lab in case["label"].items() if lab == "yes"]
    no = [k for k, lab in case["label"].items() if lab == "no"]
    misorder = sum(1 for y in yes for n in no if v.scores.get(y, 0) <= v.scores.get(n, 0))
    return first_ok, misorder


async def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "deepseek"
    cases = load_cases(Path(sys.argv[2]) if len(sys.argv) > 2 else CASES)
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT.parent / "sayaka-bot" / ".env")
    backend = "gemini" if name == "gemini" else "deepseek"
    if backend == "gemini":
        gemini_key()
    group = build_group(backend)
    for m in group.members.values():  # PROFILE 也套用到 DeepSeek 判斷（_pick 看的是 Member.profile）
        m.profile = member_profile(m)
    judge = JUDGES[name]
    import os

    tag = f"_{os.environ['PROFILE']}" if os.environ.get("PROFILE") else ""
    out = Path(__file__).with_name(f"result_{name}{tag}.md")
    rows, ok, mis, secs = [], 0, 0, []
    for case in cases:
        v = await judge(group, case)
        first_ok, misorder = grade(case, v)
        ok += first_ok
        mis += misorder
        secs.append(v.seconds)
        scores = " ".join(f"{group.members[k].name}{s:.2f}" for k, s in v.scores.items())
        first = group.members[v.first].name if v.first else "（沒人）"
        want = "、".join(group.members[k].name for k in case["first"]) or "（沒人）"
        rows.append(f"| {case['id']} | {'✅' if first_ok else '❌'} | {first} | {want} | {misorder} | {scores} |")
        print(rows[-1], flush=True)
    secs.sort()
    summary = (f"# 判斷方式：{name}\n\n第一位正確 {ok}/{len(cases)}，排序錯 {mis} 對，"
               f"延遲中位數 {secs[len(secs) // 2]:.2f} 秒\n\n"
               "| 題目 | 第一位 | 判斷 | 可接受 | 排序錯 | 分數 |\n|---|---|---|---|---|---|\n")
    out.write_text(summary + "\n".join(rows) + "\n", encoding="utf-8")
    print(summary.splitlines()[2])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
