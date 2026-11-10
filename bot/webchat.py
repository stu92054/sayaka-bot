"""網頁版聊天（測試用）：不經過 Discord，用同一套 persona、lore、回憶卡、DeepSeek 呼叫與每日費用上限。
進入點：python -m bot.webchat

環境變數（其餘沿用 bot 的設定，例如 DEEPSEEK_API_KEY、DAILY_COST_BUDGET）：
  WEBCHAT_CHARACTERS  角色資料夾名稱，逗號分隔（預設 "kosuzu,sayaka,tsuzuri"），每個角色各用自己的資料庫 data/web_<key>.sqlite3
  WEBCHAT_PASSWORD    通關密碼（必填；網頁每次送訊息都要帶）
  WEBCHAT_HOST／WEBCHAT_PORT  預設 0.0.0.0:8090
  GEMINI_API_KEY      有設定時網頁多一個「Gemini」模型開關（走 Gemini 的 OpenAI 相容端點，另存 data/web_gemini_<key>.sqlite3）
  WEBCHAT_GEMINI_MODEL／WEBCHAT_GEMINI_PRICE  預設 gemini-3.8-flash，價格「快取:輸入:輸出」每百萬 tokens（USD）

每個瀏覽器分頁是一段獨立對話（前端產生的 session id 換成 channel id），走和 Discord 私訊相同的流程。
/group 是 LINE 式的群組聊天（只用 DeepSeek），回話順序見 bot/group.py。
群組聊天的對話列表（設定＋完整對話）存在 data/web_groups.sqlite3，換裝置也能接著聊；
網頁只有一組共用密碼，知道密碼的人都看得到所有群組對話。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .chat import ChatService, display_author
from .config import Config
from .group import GroupChat, GroupError, load_member
from .group_rooms import GroupRooms
from .history import HistoryManager
from .holidays import HolidayCalendar, pricing_years
from .llm import LLMClient
from .lore import Lore
from .main import setup_logging
from .memories import Memories
from .persona import Persona
from .replies import Replies
from .storage import Storage
from .triggers import Cooldown, Trigger

log = logging.getLogger("webchat")
MAX_TEXT = 500
# .env 裡角色專屬的設定（顯示名稱、人設路徑…）是給單一角色的 Discord bot 用的，網頁版一律以角色資料夾為準
CHARACTER_KEYS = {"BOT_NAMES", "BOT_DISPLAY_NAME", "PERSONA_PATH", "REPLIES_PATH", "LORE_DIR", "MEMORIES_DIR",
                  "DB_PATH", "CHARACTER_DIR", "DISCORD_TOKEN"}


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass
class Character:
    key: str
    name: str
    services: dict[str, ChatService]  # "deepseek"、"gemini"
    storages: list[Storage]


class MergedSystem:
    """包住 AsyncOpenAI，讓 bot 的請求能送進 Gemini 的相容端點：所有 system 訊息併成一則放最前面（只認一則），
    並拿掉 DeepSeek 專用的參數。"""

    def __init__(self, client):
        self._client = client
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        # DeepSeek 專用的 thinking 參數 Gemini 不認（工具迴圈固定會帶）；改用 reasoning_effort
        kwargs.pop("extra_body", None)
        kwargs.setdefault("reasoning_effort", "low")
        msgs = kwargs.get("messages") or []
        system = [m["content"] for m in msgs if m.get("role") == "system" and isinstance(m.get("content"), str)]
        if len(system) > 1:
            kwargs["messages"] = ([{"role": "system", "content": "\n\n".join(system)}]
                                  + [m for m in msgs if m.get("role") != "system"])
        return await self._client.chat.completions.create(**kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


def build_service(key: str, backend: str) -> tuple[ChatService, Storage]:
    env = {k: v for k, v in os.environ.items() if k not in CHARACTER_KEYS}
    env.update({"CHARACTER_DIR": f"characters/{key}", "DB_PATH": f"data/web_{key}.sqlite3"})
    if backend == "gemini":
        model = os.environ.get("WEBCHAT_GEMINI_MODEL", "gemini-3.8-flash")
        env.update({"DEEPSEEK_API_KEY": os.environ["GEMINI_API_KEY"], "DEEPSEEK_BASE_URL": GEMINI_BASE_URL,
                    "MODEL_DEFAULT": model, "MODEL_DEEP": model, "THINKING_DEFAULT": "omit", "THINKING_DEEP": "omit",
                    "REASONING_EFFORT_DEFAULT": "low", "PRICE_OFFPEAK_RATIO": "1", "WEB_TOOLS_ENABLED": "false",
                    "PRICE_TABLE": f"{model}:{os.environ.get('WEBCHAT_GEMINI_PRICE', '0.075:0.75:3.75')}",
                    "DB_PATH": f"data/web_gemini_{key}.sqlite3"})
    config = Config.from_env(env, load_dotenv_file=False)
    persona = Persona(config.persona_path, names=(config.bot_display_name, *config.bot_aliases), scene="web")
    if backend == "gemini":
        persona.swap("DeepSeek", "Gemini")
    lore = Lore(config.lore_dir) if config.lore_dir is not None else None
    memories = Memories(config.memories_dir, results=config.recall_results) if config.memories_dir is not None else None
    storage = Storage(config.db_path)
    calendar = HolidayCalendar(cache_dir=config.db_path.parent / "holidays", sources=config.holiday_sources,
                               manual_off_days=config.cn_holidays)
    calendar.load_cache(pricing_years())
    llm = LLMClient(config, storage, calendar=calendar)
    history = HistoryManager(storage, llm, config.history_max_messages, config.history_timestamps)
    if backend == "gemini":
        llm.client = MergedSystem(llm.client)
    service = ChatService(config, llm, history, persona, Cooldown(config.user_cooldown_seconds),
                          replies=Replies(config.replies_path), lore=lore, memories=memories)
    log.info("載入 %s／%s（回憶卡 %d 張）", config.bot_display_name, backend, len(memories.cards) if memories else 0)
    return service, storage


def load_character(key: str) -> Character:
    backends = ["deepseek"] + (["gemini"] if os.environ.get("GEMINI_API_KEY") else [])
    built = {b: build_service(key, b) for b in backends}
    services = {b: s for b, (s, _) in built.items()}
    return Character(key, services["deepseek"].config.bot_display_name, services, [st for _, st in built.values()])


def session_id(raw: str) -> int:
    """前端的 session 字串 → 穩定的正整數（當 channel id 與冷卻用的 user id）。"""
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:7], "big")


async def reply(svc: ChatService, session: str, author: str, text: str) -> list[str]:
    channel = session_id(session)
    author = display_author(author, svc.config)
    if not svc.cooldown.try_acquire(channel):  # 先擋冷卻，被擋下的訊息不寫進歷史
        return ["（太快了，等幾秒再說～）"]
    prompt = svc.history.build_prompt(channel, svc.persona.text, author, text, svc._lore_text(), now=svc._now())
    prompt = svc._with_tool_guide(prompt)
    svc.history.add(channel, author, text)
    runner = svc._tool_runner(channel, text, prompt) if svc.tools_enabled else None
    result = await svc.generate(channel, prompt, Trigger.DM, model=svc.config.model_default, tool_runner=runner)
    if svc.history.needs_summary(channel):
        await svc.history.maybe_summarize(channel)
    return result.chunks


def make_app(chars: dict[str, Character], password: str, group: GroupChat | None = None,
             rooms: GroupRooms | None = None) -> web.Application:
    page = (Path(__file__).parent / "webchat.html").read_text(encoding="utf-8")
    group_page = (Path(__file__).parent / "webchat_group.html").read_text(encoding="utf-8")
    if group is None:
        first = next(iter(chars.values()))
        group = GroupChat({k: load_member(k, c.services["deepseek"]) for k, c in chars.items()},
                          gm=dict(first.services))  # GM 可以用 DeepSeek 或（有金鑰時）Gemini
    if rooms is None:
        rooms = GroupRooms(":memory:")

    def authorized(request: web.Request) -> bool:
        return hmac.compare_digest(request.headers.get("X-Pass", ""), password)

    async def index(_: web.Request) -> web.Response:
        options = "".join(f'<option value="{c.key}">{c.name}</option>' for c in chars.values())
        backends = next(iter(chars.values())).services
        models = "".join(f'<option value="{b}">{"DeepSeek" if b == "deepseek" else "Gemini"}</option>' for b in backends)
        return web.Response(text=page.replace("{{OPTIONS}}", options).replace("{{MODELS}}", models),
                            content_type="text/html")

    async def chat(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Pass", ""), password):
            return web.json_response({"error": "通關密碼不對"}, status=403)
        try:
            body = await request.json()
            char = chars[body["character"]]
            backend = str(body.get("model") or "deepseek")
            svc = char.services[backend]
            session, author, text = str(body["session"])[:64], str(body.get("name") or "訪客")[:20], str(body["text"])
        except (KeyError, ValueError, TypeError):
            return web.json_response({"error": "格式不對"}, status=400)
        text = text.strip()[:MAX_TEXT]
        if not text or len(session) < 8:
            return web.json_response({"error": "格式不對"}, status=400)
        messages = await reply(svc, f"{char.key}:{backend}:{session}", author, text)
        return web.json_response({"messages": messages, "name": char.name})

    async def group_index(_: web.Request) -> web.Response:
        members = [{"key": m.key, "name": m.name, "color": m.color} for m in group.members.values()]
        gms = list(group.gm) or ["deepseek"]  # 有 Gemini 金鑰時才會有 gemini
        page = group_page.replace("{{MEMBERS}}", json.dumps(members, ensure_ascii=False).replace("</", "<\\/"))
        return web.Response(text=page.replace("{{GMS}}", json.dumps(gms)), content_type="text/html")

    async def group_body(request: web.Request) -> dict | web.Response:
        if not authorized(request):
            return web.json_response({"error": "通關密碼不對"}, status=403)
        try:
            body = await request.json()
            members = [str(k) for k in body["members"]][:12]
            session = str(body["session"])[:64]
        except (KeyError, ValueError, TypeError):
            return web.json_response({"error": "格式不對"}, status=400)
        if len(session) < 8:
            return web.json_response({"error": "格式不對"}, status=400)
        return {"body": body, "members": members, "session": session}

    async def group_send(request: web.Request) -> web.Response:
        parsed = await group_body(request)
        if isinstance(parsed, web.Response):
            return parsed
        body = parsed["body"]
        text = str(body.get("text") or "").strip()[:MAX_TEXT]
        author = str(body.get("name") or "訪客").strip()[:20] or "訪客"
        if not text:
            return web.json_response({"error": "格式不對"}, status=400)
        identity, situation, time_ = (str(body.get(k) or "")[:200] for k in ("identity", "situation", "time"))
        try:
            nxt = await group.send(parsed["session"], parsed["members"], author, identity, text,
                                   situation=situation, time=time_, play=str(body.get("play") or "off"),
                                   gm=str(body.get("gm") or "deepseek"), gm_style=str(body.get("gm_style") or "short"))
        except GroupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        extra = {k: str(body.get(k) or "")[:20] for k in ("play", "gm", "gm_style")}
        rooms.save_settings(parsed["session"], parsed["members"], identity, situation, time_, author, extra)
        rooms.add_line(parsed["session"], "user", author, text)
        report = group.gm_report(parsed["session"], parsed["members"])
        if report["narration"]:
            stamp = f"[{report['time']}] " if report["time"] else ""
            rooms.add_line(parsed["session"], "narrator", "旁白", stamp + report["narration"])
        if report["ooc"]:
            rooms.add_line(parsed["session"], "gm", "GM", report["ooc"])
        return web.json_response({"next": nxt, **report})

    async def group_speak(request: web.Request) -> web.Response:
        parsed = await group_body(request)
        if isinstance(parsed, web.Response):
            return parsed
        try:
            member, messages, ok, nxt = await group.speak(parsed["session"], parsed["members"],
                                                          str(parsed["body"].get("speaker") or ""))
        except GroupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if ok and messages:
            rooms.add_line(parsed["session"], member.key, member.name, "\n".join(messages))
        return web.json_response({"key": member.key, "name": member.name, "messages": messages, "ok": ok, "next": nxt})

    async def room_list(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"error": "通關密碼不對"}, status=403)
        return web.json_response({"rooms": rooms.list()})

    async def room_get(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"error": "通關密碼不對"}, status=403)
        room = rooms.get(request.match_info["room"])
        if room is None:
            return web.json_response({"error": "找不到這個群組"}, status=404)
        return web.json_response(room)

    async def room_delete(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"error": "通關密碼不對"}, status=403)
        room_id = request.match_info["room"]
        room = rooms.get(room_id)
        if room is not None:  # 角色記得的這場對話也一起清掉，不只是列表
            group.forget(room_id, room["members"])
        return web.json_response({"deleted": rooms.delete(room_id)})

    app = web.Application(client_max_size=16 * 1024)
    app.router.add_get("/", index)
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/group", group_index)
    app.router.add_post("/api/group/send", group_send)
    app.router.add_post("/api/group/speak", group_speak)
    app.router.add_get("/api/group/rooms", room_list)
    app.router.add_get("/api/group/rooms/{room}", room_get)
    app.router.add_delete("/api/group/rooms/{room}", room_delete)
    return app


def main() -> int:
    load_dotenv()
    password = os.environ.get("WEBCHAT_PASSWORD", "")
    keys = [k.strip() for k in os.environ.get("WEBCHAT_CHARACTERS", "kosuzu,sayaka,tsuzuri").split(",") if k.strip()]
    chars = {k: load_character(k) for k in keys}
    setup_logging(next(iter(chars.values())).services["deepseek"].config)
    if len(password) < 6:
        log.error("WEBCHAT_PASSWORD 至少要 6 個字")
        return 1
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log.error("缺少 DEEPSEEK_API_KEY")
        return 1
    rooms = GroupRooms(Path("data/web_groups.sqlite3"))
    try:
        web.run_app(make_app(chars, password, rooms=rooms), host=os.environ.get("WEBCHAT_HOST", "0.0.0.0"),
                    port=int(os.environ.get("WEBCHAT_PORT", "8090")), print=None)
    finally:
        rooms.close()
        for c in chars.values():
            for st in c.storages:
                st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
