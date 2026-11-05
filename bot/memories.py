"""回憶卡（memories/*.md）載入與檢索，以及給模型呼叫的 recall 工具。

和原作資料（lore/）不同，回憶卡不整份放進 prompt：模型需要時呼叫 recall，
用 BM25 找出最相關的幾張卡，當成工具結果交給模型。
- 卡片格式：每張以「## 」開頭的標題起始，到下一個「## 」為止；「關鍵詞：」那行只用來檢索，不會回傳給模型。
- 別名表（memories/aliases.txt）：每行一組同義詞，用空白分隔；查詢含其中一個詞時，整組都加進查詢。
- 中日文沒有空白斷詞，所以切成相鄰兩字，漢字另外加單字（假名單字太常見，不加）；
  英數字以整個詞為單位，數字去掉前導 0（01 和 1 視為相同）。
- 查詢裡的虛詞（妳、的、那次……）不算分；別名展開加進來的詞權重減半，避免蓋過原本的關鍵字。
說明文字只寫用法，不帶角色名稱；角色相關內容全在 memories/ 的檔案裡。
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .tools import Tool, ToolRunner

log = logging.getLogger(__name__)

ALIASES_FILE = "aliases.txt"
KEYWORDS_PREFIX = "關鍵詞："
_CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(f"[{_CJK}]+|[0-9]+|[a-z]+")
_KANA = re.compile(r"[぀-ヿ]")
K1, B = 1.5, 0.75
ALIAS_WEIGHT = 0.5
# 問句常見的虛詞：只在查詢端略過（卡片內容照常索引）
STOP_TOKENS = frozenset("妳你我他她的了是在那這次時候什麼怎樣誰有嗎呢吧和跟個一些要去哪裡說過會做還就都也")

RECALL_GUIDE = (
    "你可以用 recall 工具查自己過去的回憶紀錄（直播、節目、活動裡發生過的事）：\n"
    "- 有人問到你過去某次直播、節目、企劃、梗，或你和成員之間某件具體的事時，先用 recall 查，不要憑印象編。"
    "原作資料只寫到大概、對方問的是細節（說了什麼、講了哪些內容、哪個詞、出了什麼題）時，也要查。\n"
    "- 要查就直接呼叫工具，不要在回覆裡寫出「[recall: …]」這類文字。\n"
    "- 查詢用具體的詞，例如成員名、企劃或單元名、歌名、梗、食物、時期（「高二 生日」「電台 最終回」這種）。\n"
    "- 查到的是你自己的經歷：用自己的話、自己的口吻自然地說，挑和問題有關的講，不要整張念出來，"
    "也不要提到「紀錄」「卡片」「工具」。\n"
    "- 查不到就說記不太清楚，不要編造。一般閒聊、打招呼不需要查。"
)

RECALL_RESULT_NOTE = "（以下是你自己的回憶紀錄，只挑和問題有關的部分，用自己的口吻回答。）"


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for run in _TOKEN_RE.findall(text.lower()):
        if run.isdigit():
            tokens.append(run.lstrip("0") or "0")
        elif run.isascii():
            tokens.append(run)
        else:
            tokens.extend(ch for ch in run if not _KANA.match(ch))
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class Card:
    title: str
    body: str  # 回傳給模型的內容（不含關鍵詞）
    tokens: Counter = field(default_factory=Counter)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


def parse_cards(content: str) -> list[Card]:
    cards = []
    for block in re.split(r"^(?=## )", content, flags=re.M):
        if not block.startswith("## "):
            continue  # 檔案開頭的說明
        lines = block.strip().splitlines()
        title = lines[0][3:].strip()
        body = [ln for ln in lines[1:] if not ln.startswith(KEYWORDS_PREFIX)]
        keywords = [ln[len(KEYWORDS_PREFIX):] for ln in lines[1:] if ln.startswith(KEYWORDS_PREFIX)]
        card = Card(title, "\n".join(body).strip())
        card.tokens = Counter(tokenize("\n".join([title, *body, *keywords])))
        cards.append(card)
    return cards


def parse_aliases(content: str) -> list[list[str]]:
    groups = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words = line.split()
            if len(words) > 1:
                groups.append(words)
    return groups


class Memories:
    def __init__(self, directory: str | Path | None, *, results: int = 3):
        self.directory = Path(directory) if directory is not None else None
        self.results = results
        self.cards: list[Card] = []
        self.files: list[str] = []
        self.aliases: list[list[str]] = []
        self.warnings: list[str] = []
        self._df: Counter = Counter()
        self._avg_len = 0.0
        if self.directory is not None:
            self.reload()

    @property
    def enabled(self) -> bool:
        return bool(self.cards)

    def reload(self) -> list[str]:
        """重新讀取資料夾內的 *.md 與別名表。讀不到任何卡片時，保留先前載入的內容。"""
        warnings: list[str] = []
        if self.directory is None:
            self.warnings = []
            return []
        cards: list[Card] = []
        names: list[str] = []
        aliases: list[list[str]] = []
        if not self.directory.is_dir():
            warnings.append(f"找不到回憶資料夾 {self.directory}")
        else:
            for path in sorted(self.directory.glob("*.md")):
                try:
                    parsed = parse_cards(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError) as exc:
                    warnings.append(f"{path.name} 讀取失敗（{type(exc).__name__}），已略過")
                    continue
                if not parsed:
                    warnings.append(f"{path.name} 裡沒有卡片（以「## 」開頭的段落），已略過")
                    continue
                cards.extend(parsed)
                names.append(path.name)
            alias_path = self.directory / ALIASES_FILE
            if alias_path.is_file():
                try:
                    aliases = parse_aliases(alias_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError) as exc:
                    warnings.append(f"{ALIASES_FILE} 讀取失敗（{type(exc).__name__}），不使用別名")
            if not cards:
                warnings.append(f"{self.directory} 裡沒有可用的卡片")

        if cards:
            self.cards, self.files, self.aliases = cards, names, aliases
            self._df = Counter(t for c in cards for t in c.tokens)
            self._avg_len = sum(sum(c.tokens.values()) for c in cards) / len(cards)
            log.info("已載入回憶卡 %d 張（%d 個檔案）", len(cards), len(names))
        elif self.cards:
            warnings.append("保留目前的回憶卡")
        else:
            warnings.append("不使用回憶卡")
        for w in warnings:
            log.warning(w)
        self.warnings = warnings
        return warnings

    def query_terms(self, query: str) -> dict[str, float]:
        """查詢詞與權重。同一個詞出現多次只算一次；別名展開加進來的詞權重較低。"""
        terms = {t: 1.0 for t in tokenize(query) if t not in STOP_TOKENS}
        q = query.lower()
        for group in self.aliases:
            if any(a.lower() in q for a in group):
                for t in tokenize(" ".join(group)):
                    terms.setdefault(t, ALIAS_WEIGHT)
        return terms

    def search(self, query: str, limit: int | None = None) -> list[Card]:
        """BM25，依查詢詞權重加總。"""
        terms = self.query_terms(query)
        n = len(self.cards)
        scored = []
        for card in self.cards:
            length = sum(card.tokens.values())
            score = 0.0
            for t, weight in terms.items():
                tf = card.tokens.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                score += weight * idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self._avg_len))
            if score > 0:
                scored.append((score, card))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[: limit or self.results]]


def format_recall(query: str, cards: list[Card]) -> str:
    if not cards:
        return f"（回憶裡找不到和「{query}」有關的事）"
    body = "\n\n".join(c.text for c in cards)
    return f"{RECALL_RESULT_NOTE}\n<memories>\n{body}\n</memories>"


def build_recall_tool(memories: Memories) -> Tool:
    async def recall(args: dict, runner: ToolRunner) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "（請提供要回想的關鍵字）"
        cards = memories.search(query)
        log.info("recall「%s」：%s", query[:50], "、".join(c.title[:30] for c in cards) or "沒有結果")
        return format_recall(query, cards)

    return Tool(
        name="recall",
        description="查你自己過去的回憶紀錄（直播、節目、活動裡發生的事），回傳最相關的幾則。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "要回想的關鍵字，用空白分隔幾個具體的詞"}},
            "required": ["query"],
        },
        handler=recall,
        counts_toward_quota=False,
    )
