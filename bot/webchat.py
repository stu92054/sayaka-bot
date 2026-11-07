"""網頁版聊天（測試用）：不經過 Discord，用同一套 persona、lore、回憶卡、DeepSeek 呼叫與每日費用上限。
進入點：python -m bot.webchat

環境變數（其餘沿用 bot 的設定，例如 DEEPSEEK_API_KEY、DAILY_COST_BUDGET）：
  WEBCHAT_CHARACTERS  角色資料夾名稱，逗號分隔（預設 "kosuzu,sayaka"），每個角色各用自己的資料庫 data/web_<key>.sqlite3
  WEBCHAT_PASSWORD    通關密碼（必填；網頁每次送訊息都要帶）
  WEBCHAT_HOST／WEBCHAT_PORT  預設 0.0.0.0:8090

每個瀏覽器分頁是一段獨立對話（前端產生的 session id 換成 channel id），走和 Discord 私訊相同的流程。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .chat import ChatService, display_author
from .config import Config
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


@dataclass
class Character:
    key: str
    name: str
    service: ChatService
    storage: Storage


def load_character(key: str) -> Character:
    env = {k: v for k, v in os.environ.items() if k not in CHARACTER_KEYS}
    env.update({"CHARACTER_DIR": f"characters/{key}", "DB_PATH": f"data/web_{key}.sqlite3"})
    config = Config.from_env(env, load_dotenv_file=False)
    persona = Persona(config.persona_path)
    lore = Lore(config.lore_dir) if config.lore_dir is not None else None
    memories = Memories(config.memories_dir, results=config.recall_results) if config.memories_dir is not None else None
    storage = Storage(config.db_path)
    calendar = HolidayCalendar(cache_dir=config.db_path.parent / "holidays", sources=config.holiday_sources,
                               manual_off_days=config.cn_holidays)
    calendar.load_cache(pricing_years())
    llm = LLMClient(config, storage, calendar=calendar)
    history = HistoryManager(storage, llm, config.history_max_messages, config.history_timestamps)
    service = ChatService(config, llm, history, persona, Cooldown(config.user_cooldown_seconds),
                          replies=Replies(config.replies_path), lore=lore, memories=memories)
    log.info("載入 %s（回憶卡 %d 張）", config.bot_display_name, len(memories.cards) if memories else 0)
    return Character(key, config.bot_display_name, service, storage)


def session_id(raw: str) -> int:
    """前端的 session 字串 → 穩定的正整數（當 channel id 與冷卻用的 user id）。"""
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:7], "big")


async def reply(char: Character, session: str, author: str, text: str) -> list[str]:
    svc = char.service
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


def make_app(chars: dict[str, Character], password: str) -> web.Application:
    page = (Path(__file__).parent / "webchat.html").read_text(encoding="utf-8")

    async def index(_: web.Request) -> web.Response:
        options = "".join(f'<option value="{c.key}">{c.name}</option>' for c in chars.values())
        return web.Response(text=page.replace("{{OPTIONS}}", options), content_type="text/html")

    async def chat(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Pass", ""), password):
            return web.json_response({"error": "通關密碼不對"}, status=403)
        try:
            body = await request.json()
            char = chars[body["character"]]
            session, author, text = str(body["session"])[:64], str(body.get("name") or "訪客")[:20], str(body["text"])
        except (KeyError, ValueError, TypeError):
            return web.json_response({"error": "格式不對"}, status=400)
        text = text.strip()[:MAX_TEXT]
        if not text or len(session) < 8:
            return web.json_response({"error": "格式不對"}, status=400)
        messages = await reply(char, f"{char.key}:{session}", author, text)
        return web.json_response({"messages": messages, "name": char.name})

    app = web.Application(client_max_size=16 * 1024)
    app.router.add_get("/", index)
    app.router.add_post("/api/chat", chat)
    return app


def main() -> int:
    load_dotenv()
    password = os.environ.get("WEBCHAT_PASSWORD", "")
    keys = [k.strip() for k in os.environ.get("WEBCHAT_CHARACTERS", "kosuzu,sayaka").split(",") if k.strip()]
    chars = {k: load_character(k) for k in keys}
    setup_logging(next(iter(chars.values())).service.config)
    if len(password) < 6:
        log.error("WEBCHAT_PASSWORD 至少要 6 個字")
        return 1
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log.error("缺少 DEEPSEEK_API_KEY")
        return 1
    try:
        web.run_app(make_app(chars, password), host=os.environ.get("WEBCHAT_HOST", "0.0.0.0"),
                    port=int(os.environ.get("WEBCHAT_PORT", "8090")), print=None)
    finally:
        for c in chars.values():
            c.storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
