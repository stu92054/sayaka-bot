"""SQLite 儲存：對話歷史、頻道摘要、API 用量。"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);

CREATE TABLE IF NOT EXISTS summaries (
    channel_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    tool TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_usage_day_user ON tool_usage(day, user_id);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    created_at REAL NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cache_hit_tokens INTEGER NOT NULL,
    cache_miss_tokens INTEGER NOT NULL,
    peak INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'CNY'
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day);
"""


@dataclass(frozen=True)
class StoredMessage:
    id: int
    channel_id: int
    author_name: str
    content: str
    is_bot: bool
    created_at: float | None = None  # Unix 時間戳


@dataclass(frozen=True)
class UsageRow:
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    cost: float = 0.0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Storage:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(usage)")}
            for col, ddl in (("peak", "INTEGER NOT NULL DEFAULT 0"), ("cost", "REAL NOT NULL DEFAULT 0"),
                             ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
                             # 加入幣別前的紀錄都是以人民幣計算
                             ("currency", "TEXT NOT NULL DEFAULT 'CNY'")):
                if col not in cols:  # 舊版資料庫升級
                    self._conn.execute(f"ALTER TABLE usage ADD COLUMN {col} {ddl}")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- messages ---
    def add_message(self, channel_id: int, author_name: str, content: str, is_bot: bool = False) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(channel_id, author_name, content, is_bot, created_at) VALUES (?,?,?,?,?)",
                (channel_id, author_name, content, int(is_bot), time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def count_messages(self, channel_id: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()
        return int(row[0])

    def recent_messages(self, channel_id: int, limit: int) -> list[StoredMessage]:
        """最新的 limit 則，依時間由舊到新排列。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, channel_id, author_name, content, is_bot, created_at FROM messages "
                "WHERE channel_id=? ORDER BY id DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [StoredMessage(r[0], r[1], r[2], r[3], bool(r[4]), r[5]) for r in reversed(rows)]

    def oldest_messages(self, channel_id: int, limit: int) -> list[StoredMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, channel_id, author_name, content, is_bot, created_at FROM messages "
                "WHERE channel_id=? ORDER BY id ASC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [StoredMessage(r[0], r[1], r[2], r[3], bool(r[4]), r[5]) for r in rows]

    def delete_messages_upto(self, channel_id: int, last_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE channel_id=? AND id<=?", (channel_id, last_id))
            self._conn.commit()

    # --- summaries ---
    def get_summary(self, channel_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT summary FROM summaries WHERE channel_id=?", (channel_id,)).fetchone()
        return row[0] if row else None

    def set_summary(self, channel_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries(channel_id, summary, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                (channel_id, summary, time.time()),
            )
            self._conn.commit()

    def clear_channel(self, channel_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE channel_id=?", (channel_id,))
            self._conn.execute("DELETE FROM summaries WHERE channel_id=?", (channel_id,))
            self._conn.commit()

    # --- usage ---
    def record_usage(
        self,
        day: str,
        model: str,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
        cost: float = 0.0,
        peak: bool = False,
        reasoning_tokens: int = 0,
        currency: str = "USD",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage(day, created_at, model, purpose, prompt_tokens, completion_tokens, "
                "cache_hit_tokens, cache_miss_tokens, peak, cost, reasoning_tokens, currency) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, time.time(), model, purpose, prompt_tokens, completion_tokens, cache_hit_tokens,
                 cache_miss_tokens, int(peak), cost, reasoning_tokens, currency),
            )
            self._conn.commit()

    def tokens_on(self, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) FROM usage WHERE day=?", (day,)
            ).fetchone()
        return int(row[0])

    # --- 工具使用次數（每人每日上限用） ---
    def record_tool_use(self, day: str, user_id: int, tool: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO tool_usage(day, user_id, tool, created_at) VALUES (?,?,?,?)",
                               (day, user_id, tool, time.time()))
            self._conn.commit()

    def tool_uses_on(self, day: str, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM tool_usage WHERE day=? AND user_id=?",
                                     (day, user_id)).fetchone()
        return int(row[0])

    def cost_on(self, day: str, currency: str | None = None) -> float:
        """當天費用加總；指定 currency 時只加總該幣別（不同幣別不能相加）。"""
        sql, args = "SELECT COALESCE(SUM(cost), 0) FROM usage WHERE day=?", [day]
        if currency is not None:
            sql += " AND currency=?"
            args.append(currency)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return float(row[0])

    def usage_by_model(self, day: str, currency: str | None = None) -> list[UsageRow]:
        """依模型彙總當天用量。指定 currency 時，費用只加總該幣別的紀錄（token 數照常全部加總）。"""
        cost_expr = "SUM(cost)" if currency is None else "SUM(CASE WHEN currency=? THEN cost ELSE 0 END)"
        args = ([currency] if currency is not None else []) + [day]
        with self._lock:
            rows = self._conn.execute(
                "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(cache_hit_tokens), "
                f"SUM(cache_miss_tokens), {cost_expr}, SUM(reasoning_tokens) FROM usage WHERE day=? "
                "GROUP BY model ORDER BY model",
                args,
            ).fetchall()
        return [UsageRow(r[0], int(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5]), float(r[6]), int(r[7])) for r in rows]
