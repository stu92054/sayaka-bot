"""網頁版的 LINE 式群組聊天：使用者和幾個角色在同一個聊天室，誰回話、誰先回由這裡決定。

每個角色仍用自己的 persona、lore、回憶卡與資料庫；同一個聊天室在每個角色的資料庫裡用同一個 channel id，
任何人說的話都抄進每個角色的歷史（她自己說的記成 bot 訊息，別人說的記成「名字: 內容」）。

回話順序：
1. 使用者點名（名字、別名、call_names）→ 依點名順序回；再問模型旁邊有沒有人想插一句（可以沒有）。
2. 沒點名 → 用一般模型判斷誰最自然會接話（1～2 人）。
3. 角色回話時點名了別的成員 → 被點名的人接著回；沒點名 → 問模型有沒有人想接一句（可以沒有）。
判斷時模型會看到每位成員的 group_profile（character.toml），才知道誰對什麼話題有反應。
使用者每說一句，角色最多回 max_replies 則，避免角色之間一直聊下去。
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
import tomllib
from dataclasses import dataclass, field
from typing import Callable

from .chat import ChatService, display_author
from .history import build_prompt
from .llm import BudgetExceeded, ContentFiltered, LLMError
from .triggers import Trigger

log = logging.getLogger(__name__)

MAX_REPLIES = 3
MAX_IDENTITY = 40

IDENTITY_PRESETS = {
    "fan": "粉絲：看你們的直播、聽歌、去 Live 的觀眾。像在 With×MEETS 和粉絲聊天那樣親切、會道謝，但保持偶像和粉絲的距離。",
    "producer": "製作人：和社團一起做企劃、宣傳和 Live 的工作夥伴（大人）。信任對方，會聊練習、企劃和舞台的事，有禮貌但不生疏。",
    "classmate": "同學：蓮之空的學生、社團外的朋友。用對同校朋友的輕鬆語氣，聊學校生活。",
}

PICK_SYSTEM = (
    "你在幫 LINE 群組決定接下來誰說話。真實的群組不是一對一：有人在聊時，其他人常會插一句附和、吐槽、關心或補充；"
    "但也不是每句話每個人都要回。依成員個性和最近的對話判斷。只輸出名字（多個用頓號分隔）或「沒有」，不要其他文字。"
)
PICK_OPEN = ("使用者剛說完。誰會回？選 1～2 個人，先回的放前面；話題和兩個人都有關才選 2 個。"
             "使用者如果是在追問或回應某人剛說的話，就先選那個人。")
PICK_EXTRA = ("{must}一定會回。除此之外，{cands}之中有沒有人會想插一句？"
              "話題和她有關、或她很久沒說話而有話可說，就輸出她的名字（最多 1 個）；沒有就輸出「沒有」。")
PICK_AFTER = ("{me}剛說完。{cands}之中有沒有人會接著說一句（回應、吐槽、補充）？最多 1 個。"
              "{me}如果是在問使用者問題（在等使用者回答），一定輸出「沒有」；"
              "大家都已經回過、話題收尾了，或接話會顯得多餘，也輸出「沒有」。")


@dataclass
class Member:
    key: str
    name: str
    color: str
    names: tuple[str, ...]  # 點名用：display_name、aliases、call_names
    svc: ChatService
    profile: str = ""  # 給「誰回話」判斷用的一兩句個性


def load_member(key: str, svc: ChatService) -> Member:
    data: dict = {}
    path = svc.config.character_dir / "character.toml" if svc.config.character_dir else None
    if path is not None and path.is_file():
        with path.open("rb") as f:
            data = tomllib.load(f)
    names = [svc.bot_display_name, *svc.config.bot_aliases, *data.get("call_names", [])]
    return Member(key, svc.bot_display_name, str(data.get("color", "#8a8178")),
                  tuple(dict.fromkeys(n for n in names if n)), svc, str(data.get("group_profile", "")).strip())


def find_mentions(text: str, members: list[Member]) -> list[Member]:
    """文字裡點到名的成員，依第一次出現的位置排序。"""
    low = text.casefold()
    found = []
    for m in members:
        hits = [low.find(n.casefold()) for n in m.names]
        hits = [h for h in hits if h >= 0]
        if hits:
            found.append((min(hits), m))
    return [m for _, m in sorted(found, key=lambda x: x[0])]


def strip_other_speakers(text: str, names: list[str]) -> str:
    """模型替別人寫台詞（「沙耶香: …」）時，從那一行開始全部拿掉。"""
    names = [n for n in names if n]
    if not names:
        return text
    pattern = re.compile(r"^\s*(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\s*[:：]")
    kept = []
    for line in text.splitlines():
        if pattern.match(line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


@dataclass
class Turn:
    author: str
    identity: str
    queue: list[str] = field(default_factory=list)
    left: int = MAX_REPLIES
    last: str | None = None


class GroupError(ValueError):
    pass


class GroupChat:
    def __init__(self, members: dict[str, Member], max_replies: int = MAX_REPLIES,
                 rng: Callable[[], float] = random.random):
        self.members = members
        self.max_replies = max_replies
        self.rng = rng
        self.turns: dict[int, Turn] = {}

    def room(self, keys: list[str]) -> list[Member]:
        keys = list(dict.fromkeys(keys))
        if not keys or any(k not in self.members for k in keys):
            raise GroupError("成員不對")
        return [self.members[k] for k in keys]

    @staticmethod
    def channel(session: str, room: list[Member]) -> int:
        raw = "group:" + session + ":" + ",".join(sorted(m.key for m in room))
        return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:7], "big")

    @staticmethod
    def identity_text(identity: str) -> str:
        identity = identity.strip()
        return IDENTITY_PRESETS.get(identity, identity[:MAX_IDENTITY]) or IDENTITY_PRESETS["fan"]

    async def send(self, session: str, keys: list[str], author: str, identity: str, text: str) -> str | None:
        """使用者說了一句：寫進每個角色的歷史，決定第一個回話的人（回傳 key；冷卻中丟 GroupError）。"""
        room = self.room(keys)
        ch = self.channel(session, room)
        if not room[0].svc.cooldown.try_acquire(ch):
            raise GroupError("太快了，等幾秒再說～")
        for m in room:  # 暱稱撞到任何一位成員的名字，都標成「（成員暱稱）」
            shown = display_author(author, m.svc.config)
            if shown != author:
                author = shown
                break
        for m in room:
            m.svc.history.add(ch, author, text)
        for m in room:
            if m.svc.history.needs_summary(ch):
                await m.svc.history.maybe_summarize(ch)
        who = (author, self.identity_text(identity))
        first = find_mentions(text, room)
        if not first:
            first = await self._pick(room, ch, PICK_OPEN, limit=2, user=who)
        elif len(first) < len(room):
            rest = [m for m in room if m not in first]
            first += await self._pick(room, ch, PICK_EXTRA.format(
                must="、".join(m.name for m in first), cands="、".join(m.name for m in rest)), rest, limit=1, user=who)
        turn = Turn(*who, [m.key for m in first], self.max_replies)
        self.turns[ch] = turn
        return turn.queue[0] if turn.queue else None

    async def speak(self, session: str, keys: list[str], key: str) -> tuple[Member, list[str], bool, str | None]:
        """輪到 key 說話：回傳（成員、訊息、是否為模型產生、下一個回話的 key）。"""
        room = self.room(keys)
        ch = self.channel(session, room)
        turn = self.turns.get(ch)
        if turn is None or not turn.queue or turn.queue[0] != key:
            raise GroupError("還沒輪到她")
        turn.queue.pop(0)
        me = self.members[key]
        others = [m for m in room if m is not me]
        prompt = self._prompt(me, others, ch, turn)
        svc = me.svc
        runner = svc._tool_runner(ch, prompt[-1]["content"], prompt) if svc.tools_enabled else None
        result = await svc.generate(
            ch, prompt, Trigger.DM, model=svc.config.model_default, tool_runner=runner,
            clean=lambda t: strip_other_speakers(t, [m.name for m in others] + [turn.author]))
        turn.left -= 1
        turn.last = key
        if not result.recorded:
            turn.queue.clear()
            return me, result.chunks, False, None
        text = svc.history.recent(ch)[-1].content
        for m in others:
            m.svc.history.add(ch, me.name, text)
        for m in reversed(find_mentions(text, others)):  # 被點名的人插到最前面
            if m.key in turn.queue:
                turn.queue.remove(m.key)
            turn.queue.insert(0, m.key)
        while turn.queue and turn.queue[0] == key:
            turn.queue.pop(0)
        if turn.left <= 0:
            turn.queue.clear()
        elif not turn.queue and others:  # 沒人排隊：問有沒有人想接一句
            turn.queue = [m.key for m in await self._pick(room, ch, PICK_AFTER.format(
                me=me.name, cands="、".join(m.name for m in others)), others, limit=1,
                user=(turn.author, turn.identity))]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return me, lines, True, turn.queue[0] if turn.queue else None

    def _prompt(self, me: Member, others: list[Member], ch: int, turn: Turn) -> list[dict[str, str]]:
        svc = me.svc
        recent = svc.history.recent(ch)
        prior, last = recent[:-1], recent[-1]
        others_text = "、".join(m.name for m in others) if others else "（沒有其他成員）"
        audience = f"使用者「{turn.author}」在這個群組裡的身分：{turn.identity}"
        system = svc.persona.compose("group", audience, others=others_text, who=turn.author)
        prompt = build_prompt(system, svc.history.summary(ch), prior, last.author_name, last.content,
                              svc._lore_text(), now=svc._now(), timestamps=svc.history.timestamps)
        prompt = svc._with_tool_guide(prompt)
        note = f"（輪到你（{me.name}）在群組裡說話。）"
        if find_mentions(turn.identity, [me]):  # 身分寫的是和她的關係：輪到她時再提醒一次
            note = f"（輪到你（{me.name}）在群組裡說話。別忘了「{turn.author}」的身分：{turn.identity}）"
        prompt[-1] = {"role": prompt[-1]["role"], "content": prompt[-1]["content"] + "\n" + note}
        return prompt

    async def _pick(self, room: list[Member], ch: int, question: str,
                    candidates: list[Member] | None = None, limit: int = 1,
                    user: tuple[str, str] | None = None) -> list[Member]:
        """請模型判斷誰會說話。PICK_OPEN（一定要有人）失敗就隨機挑一個；其他問題可以回答沒有。"""
        cands = room if candidates is None else candidates
        must_answer = question is PICK_OPEN
        if len(room) == 1:
            return list(cands) if must_answer else []
        svc = room[0].svc
        lines = "\n".join(f"{m.author_name}: {m.content}" for m in svc.history.recent(ch)[-8:])
        profiles = "\n".join(f"- {m.name}：{m.profile}" if m.profile else f"- {m.name}" for m in room)
        if user:  # 使用者自己填的身分，讓判斷知道誰和對方比較熟
            profiles += f"\n- 使用者「{user[0]}」的身分：{user[1]}"
        prompt = [{"role": "system", "content": PICK_SYSTEM},
                  {"role": "user", "content": f"成員：\n{profiles}\n\n最近的對話：\n{lines}\n\n{question}"}]
        try:
            out = await svc.llm.chat(prompt, model=svc.config.model_default, max_tokens=20, purpose="group_pick",
                                     thinking="disabled", total_timeout=20)
        except (LLMError, BudgetExceeded, ContentFiltered) as exc:
            log.warning("判斷誰回話失敗：%s", exc)
            out = ""
        picked = find_mentions(out, cands)[:limit]
        if picked or not must_answer:
            return picked
        return [cands[int(self.rng() * len(cands)) % len(cands)]]
