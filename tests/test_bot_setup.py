from datetime import datetime

import discord

from bot.llm import LLMClient
from bot.main import TsundereBot
from bot.persona import Persona

from .conftest import TAIPEI, make_config


async def test_bot_constructs_with_commands_and_safe_mentions(storage, fake_openai, persona_file):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, tzinfo=TAIPEI))
    bot = TsundereBot(config, storage, llm, Persona(persona_file))
    try:
        names = {c.name for c in bot.tree.get_commands()}
        assert names == {"deep", "reset", "persona_reload", "usage"}
        am = bot.allowed_mentions
        assert am.everyone is False and am.roles is False
        assert bot.intents.message_content
    finally:
        await bot.close()


async def test_dm_allowed_for_admin_without_network(storage, fake_openai, persona_file):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai)
    bot = TsundereBot(config, storage, llm, Persona(persona_file))
    try:
        assert await bot.dm_allowed(7)  # ADMIN_ID
        assert not await bot.dm_allowed(12345)  # 沒有連上任何伺服器
    finally:
        await bot.close()
