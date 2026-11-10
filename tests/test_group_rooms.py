"""網頁群組聊天的對話列表（bot/group_rooms.py 與 /api/group/rooms）。"""
from aiohttp.test_utils import TestClient, TestServer

from bot.group_rooms import GroupRooms
from bot.webchat import make_app

from .test_group import KEYS, group  # noqa: F401  （group fixture）

PASS = "secret-pass"


def test_databases_overwrite_deleted_rows():
    from bot.storage import Storage
    rooms, storage = GroupRooms(":memory:"), Storage(":memory:")
    assert rooms.db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert storage._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    rooms.close(); storage.close()


def test_rooms_keep_settings_lines_and_order():
    t = iter(range(100, 200))
    rooms = GroupRooms(":memory:", clock=lambda: next(t))
    rooms.save_settings("room-a", KEYS, "fan", "oden", "", "KO")
    rooms.add_line("room-a", "user", "KO", "開動吧")
    rooms.add_line("room-a", "kosuzu", "小鈴", "（夾起蘿蔔）\n好燙！")
    rooms.save_settings("room-b", ["tsuzuri"], "我是綴理的同學", "custom scene", "聖誕節晚上", "KO",
                        {"play": "fun", "gm": "gemini", "gm_style": "trpg"})
    listed = rooms.list()
    assert [r["id"] for r in listed] == ["room-b", "room-a"]  # 最近更新的在前面
    assert listed[0]["extra"] == {"play": "fun", "gm": "gemini", "gm_style": "trpg"} and listed[1]["extra"] == {}
    assert listed[1]["last"] == "小鈴：（夾起蘿蔔）\n好燙！" and listed[1]["count"] == 2
    a = rooms.get("room-a")
    assert a["members"] == KEYS and a["situation"] == "oden"
    assert [(x["speaker"], x["text"]) for x in a["lines"]] == [("user", "開動吧"), ("kosuzu", "（夾起蘿蔔）\n好燙！")]
    assert rooms.delete("room-a") and rooms.get("room-a") is None and not rooms.delete("room-a")
    assert rooms.db.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0  # 對話一起刪掉
    rooms.close()


async def test_api_saves_chat_and_lists_it(group):  # noqa: F811
    rooms = GroupRooms(":memory:")
    client = TestClient(TestServer(make_app({}, PASS, group=group, rooms=rooms)))
    await client.start_server()
    try:
        head = {"X-Pass": PASS}
        body = {"session": "room-api-01", "members": KEYS}
        group.client.completions.replies = ["沒有", "嗯。好冷。\n要喝熱的嗎。", "沒有"]
        r = await client.post("/api/group/send", json={**body, "name": "KO", "identity": "fan",
                                                         "situation": "oden", "time": "冬天晚上", "text": "綴理，好冷喔"},
                              headers=head)
        assert (await r.json())["next"] == "tsuzuri"
        r = await client.post("/api/group/speak", json={**body, "speaker": "tsuzuri"}, headers=head)
        assert (await r.json())["messages"] == ["嗯。好冷。", "要喝熱的嗎。"]

        listed = (await (await client.get("/api/group/rooms", headers=head)).json())["rooms"]
        assert [(x["id"], x["situation"], x["time"], x["count"]) for x in listed] == [("room-api-01", "oden", "冬天晚上", 2)]
        got = await (await client.get("/api/group/rooms/room-api-01", headers=head)).json()
        assert [(x["speaker"], x["text"]) for x in got["lines"]] == [("user", "綴理，好冷喔"),
                                                                    ("tsuzuri", "嗯。好冷。\n要喝熱的嗎。")]

        assert (await client.get("/api/group/rooms", headers={"X-Pass": "wrong"})).status == 403
        assert (await client.get("/api/group/rooms/nope", headers=head)).status == 404
        ch = group.channel_of("room-api-01", KEYS)
        assert all(group.members[k].svc.history.recent(ch) for k in KEYS)  # 每位成員都記得這場對話
        r = await client.delete("/api/group/rooms/room-api-01", headers=head)
        assert (await r.json())["deleted"] is True
        # 刪除群組時，角色的記憶（訊息與摘要）也一起清掉
        assert all(not group.members[k].svc.history.recent(ch) for k in KEYS)
        assert all(group.members[k].svc.history.summary(ch) is None for k in KEYS)
        assert (await (await client.get("/api/group/rooms", headers=head)).json())["rooms"] == []
    finally:
        await client.close()
        rooms.close()


def test_old_room_database_gets_extra_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)  # 沒有 extra 欄位的舊版資料表
    db.executescript("CREATE TABLE rooms (id TEXT PRIMARY KEY, members TEXT NOT NULL, identity TEXT NOT NULL DEFAULT '',"
                     " situation TEXT NOT NULL DEFAULT '', time TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',"
                     " created REAL NOT NULL, updated REAL NOT NULL);"
                     "INSERT INTO rooms VALUES ('old', '[\"sayaka\"]', 'fan', 'line', '', 'KO', 1, 1);")
    db.close()
    rooms = GroupRooms(path)
    assert rooms.get("old")["extra"] == {}
    rooms.close()

