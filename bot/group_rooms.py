"""網頁群組聊天的對話列表：每場聊天的設定與完整對話紀錄（給畫面顯示、接著聊用）。

角色的記憶仍在各自的資料庫（data/web_<key>.sqlite3），舊訊息會被壓成摘要；
這裡另外存一份原樣的對話，存在 data/web_groups.sqlite3。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    members TEXT NOT NULL,
    identity TEXT NOT NULL DEFAULT '',
    situation TEXT NOT NULL DEFAULT '',
    time TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS lines_room ON lines(room, id);
"""

LIST_LIMIT = 100


class GroupRooms:
    def __init__(self, path: str | Path, clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA secure_delete = ON")  # 刪掉的對話直接覆寫，不留在檔案裡
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(rooms)")}
        if "extra" not in cols:  # 舊版資料庫升級：導演模式的設定
            self.db.execute("ALTER TABLE rooms ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()
        self.clock = clock

    def close(self) -> None:
        self.db.close()

    def save_settings(self, room: str, members: list[str], identity: str, situation: str, time_: str,
                      name: str, extra: dict | None = None) -> None:
        """每次使用者說話時更新這場聊天的設定（第一次就建立）。extra：玩法、GM 模型、旁白風格。"""
        now = self.clock()
        self.db.execute(
            "INSERT INTO rooms (id, members, identity, situation, time, name, created, updated, extra)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET members=excluded.members, identity=excluded.identity,"
            " situation=excluded.situation, time=excluded.time, name=excluded.name, updated=excluded.updated,"
            " extra=excluded.extra",
            (room, json.dumps(members), identity, situation, time_, name, now, now,
             json.dumps(extra or {}, ensure_ascii=False)))
        self.db.commit()

    def add_line(self, room: str, speaker: str, author: str, text: str) -> None:
        """speaker：成員 key，使用者是 "user"。"""
        now = self.clock()
        self.db.execute("INSERT INTO lines (room, speaker, author, text, created) VALUES (?, ?, ?, ?, ?)",
                        (room, speaker, author, text, now))
        self.db.execute("UPDATE rooms SET updated = ? WHERE id = ?", (now, room))
        self.db.commit()

    def list(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.*, (SELECT author || '：' || text FROM lines WHERE room = r.id ORDER BY id DESC LIMIT 1) AS last,"
            " (SELECT COUNT(*) FROM lines WHERE room = r.id) AS count"
            " FROM rooms r ORDER BY updated DESC LIMIT ?", (LIST_LIMIT,)).fetchall()
        return [self._room(r) | {"last": r["last"] or "", "count": r["count"]} for r in rows]

    def get(self, room: str) -> dict | None:
        r = self.db.execute("SELECT * FROM rooms WHERE id = ?", (room,)).fetchone()
        if r is None:
            return None
        lines = self.db.execute("SELECT speaker, author, text, created FROM lines WHERE room = ? ORDER BY id",
                                (room,)).fetchall()
        return self._room(r) | {"lines": [dict(x) for x in lines]}

    def delete(self, room: str) -> bool:
        cur = self.db.execute("DELETE FROM rooms WHERE id = ?", (room,))
        self.db.commit()
        return cur.rowcount > 0

    @staticmethod
    def _room(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "members": json.loads(r["members"]), "identity": r["identity"],
                "situation": r["situation"], "time": r["time"], "name": r["name"],
                "created": r["created"], "updated": r["updated"], "extra": json.loads(r["extra"] or "{}")}
