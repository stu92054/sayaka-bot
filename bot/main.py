"""進入點：python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord import app_commands

from .chat import ChatService
from .commands import register_commands
from .config import Config
from .history import HistoryManager
from .holidays import HolidayCalendar, pricing_years
from .llm import LLMClient
from .lore import Lore
from .persona import Persona
from .replies import Replies
from .storage import Storage
from .triggers import Cooldown, detect_trigger

log = logging.getLogger("bot")


class RedactSecretsFilter(logging.Filter):
    """把 log 中出現的 token / API key 換成 ***。"""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        redacted = self.redact(msg)
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup_logging(config: Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactSecretsFilter(config.secrets()))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class PersonaBot(discord.Client):
    def __init__(self, config: Config, storage: Storage, llm: LLMClient, persona: Persona,
                 replies: Replies | None = None, lore: Lore | None = None):
        intents = discord.Intents.default()
        intents.message_content = True  # 需要在 Developer Portal 開啟 Message Content Intent
        # 完全不允許提及（@everyone、@here、角色、使用者、被回覆者），避免模型照著使用者要求輸出 <@id> 去 ping 人。
        # 透過 Client.allowed_mentions 屬性存取，也是這個 client 所有訊息的預設值。
        mentions = discord.AllowedMentions.none()
        super().__init__(intents=intents, allowed_mentions=mentions)
        self.config = config
        self.storage = storage
        self.llm = llm
        self.persona = persona
        self.replies = replies or Replies()
        self.lore = lore
        self.history = HistoryManager(storage, llm, config.history_max_messages, config.history_timestamps)
        self.chat = ChatService(config, llm, self.history, persona, Cooldown(config.user_cooldown_seconds),
                                replies=self.replies, lore=lore)
        self.tree = app_commands.CommandTree(self)
        self._member_cache: dict[int, bool] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        register_commands(self)

    async def setup_hook(self) -> None:
        cal = self.llm.calendar
        if cal is not None and cal.sources:
            # 背景下載，不拖慢登入；啟動時先用本機快取
            task = asyncio.create_task(cal.run_periodic_refresh(pricing_years, run_immediately=True))
            self._bg_tasks.add(task)
        for gid in self.config.allowed_guild_ids:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                log.info("已同步 slash commands 到伺服器 %s", gid)
            except discord.HTTPException as exc:
                log.warning("同步 slash commands 到 %s 失敗：%s", gid, exc)

    async def on_ready(self) -> None:
        log.info("登入為 %s（id=%s）", self.user, self.user.id if self.user else "?")

    async def dm_allowed(self, user_id: int) -> bool:
        """私訊僅限管理員或同在允許伺服器中的成員。"""
        if user_id in self.config.admin_user_ids:
            return True
        if user_id in self._member_cache:
            return self._member_cache[user_id]
        allowed = False
        for gid in self.config.allowed_guild_ids:
            guild = self.get_guild(gid)
            if guild is None:
                continue
            if guild.get_member(user_id) is not None:
                allowed = True
                break
            try:
                await guild.fetch_member(user_id)
                allowed = True
                break
            except discord.NotFound:
                continue
            except discord.HTTPException as exc:
                log.warning("查詢成員失敗：%s", exc)
        self._member_cache[user_id] = allowed
        return allowed

    def schedule_summary(self, channel_id: int) -> None:
        if not self.history.needs_summary(channel_id):
            return
        task = asyncio.create_task(self.history.maybe_summarize(channel_id))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.author.bot:
            return
        try:
            dm_ok = False
            if message.guild is None:
                dm_ok = await self.dm_allowed(message.author.id)
            # 回覆的訊息若不在快取，嘗試抓回來判斷是不是回覆 bot
            ref = message.reference
            if ref is not None and ref.resolved is None and ref.message_id:
                try:
                    ref.resolved = await message.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    pass

            reply = await self._handle_with_typing(message, dm_ok)
            if reply:
                for i, chunk in enumerate(reply.chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False, allowed_mentions=self.allowed_mentions)
                    else:
                        await message.channel.send(chunk, allowed_mentions=self.allowed_mentions)
            if message.guild is None and not dm_ok:
                return
            self.schedule_summary(message.channel.id)
        except Exception:  # noqa: BLE001 - 不讓單一訊息讓 bot 崩潰
            log.exception("處理訊息時發生錯誤")

    async def _handle_with_typing(self, message: discord.Message, dm_ok: bool):
        # 先判斷是否會觸發，觸發才顯示「輸入中…」（插話要等判斷後才知道，不顯示）
        will_trigger = detect_trigger(message, self.user.id, self.config, dm_allowed=dm_ok, rng=lambda: 1.0)
        if will_trigger is None:
            return await self.chat.handle_message(message, self.user.id, dm_allowed=dm_ok)
        async with message.channel.typing():
            return await self.chat.handle_message(message, self.user.id, dm_allowed=dm_ok)



TsundereBot = PersonaBot  # 舊名稱，供既有測試使用


def main() -> int:
    config = Config.from_env()
    setup_logging(config)
    problems = config.validate()
    fatal = [p for p in problems if "TOKEN" in p or "API_KEY" in p]
    for p in problems:
        log.warning("設定問題：%s", p)
    if fatal:
        log.error("缺少必要設定，請複製 .env.example 為 .env 並填寫")
        return 1
    log.info("設定：%r", config)

    persona = Persona(config.persona_path)
    replies = Replies(config.replies_path)  # 缺檔或缺項只記 warning，不會中止
    lore = Lore(config.lore_dir) if config.lore_dir is not None else None
    storage = Storage(config.db_path)
    calendar = HolidayCalendar(
        cache_dir=config.db_path.parent / "holidays",
        sources=config.holiday_sources,
        manual_off_days=config.cn_holidays,
    )
    calendar.load_cache(pricing_years())
    llm = LLMClient(config, storage, calendar=calendar)
    bot = PersonaBot(config, storage, llm, persona, replies, lore)
    try:
        bot.run(config.discord_token, log_handler=None)
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
