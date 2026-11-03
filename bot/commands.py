"""Slash commands：/deep、/reset、/persona_reload、/usage。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .text import split_message

if TYPE_CHECKING:
    from .main import TsundereBot

log = logging.getLogger(__name__)

NOT_ADMIN_REPLY = "哼，你沒有資格對本小姐下這種命令。"
NOT_ALLOWED_REPLY = "本小姐不在這裡營業。"


def is_admin(bot: "TsundereBot", user_id: int) -> bool:
    return user_id in bot.config.admin_user_ids


def format_usage(bot: "TsundereBot") -> str:
    rows = bot.llm.usage_today()
    total = sum(r.total_tokens for r in rows)
    budget = bot.config.daily_token_budget
    lines = [f"**今日用量（{bot.llm.today()}）**", f"總 tokens：{total:,} / {budget:,}（{total / budget:.1%}）"
             if budget else f"總 tokens：{total:,}"]
    for r in rows:
        hit_rate = r.cache_hit_tokens / r.prompt_tokens if r.prompt_tokens else 0
        lines.append(
            f"- `{r.model}`：{r.calls} 次，輸入 {r.prompt_tokens:,}（快取命中 {r.cache_hit_tokens:,}，{hit_rate:.0%}），"
            f"輸出 {r.completion_tokens:,}"
            + (f"（推理 {r.reasoning_tokens:,}）" if r.reasoning_tokens else "")
            + f"，約 ¥{r.cost:.4f}"
        )
    lines.append(f"預估費用：約 ¥{bot.llm.estimate_cost(rows):.4f}（依 DeepSeek 價目表，已區分高峰／空閒時段）")
    lines.append("目前為 **高峰時段**（價格 ×1）" if bot.llm.is_peak()
                 else f"目前為空閒時段（價格 ×{bot.config.offpeak_ratio:g}）")
    cal = bot.llm.calendar
    if cal is not None:
        years = cal.loaded_years()
        lines.append(f"節假日資料：{', '.join(map(str, years)) if years else '尚未載入（只用 CN_HOLIDAYS）'}")
    return "\n".join(lines)


def register_commands(bot: "TsundereBot") -> None:
    tree = bot.tree

    async def _guard(interaction: discord.Interaction, *, admin: bool) -> bool:
        guild = interaction.guild
        if guild is not None and guild.id not in bot.config.allowed_guild_ids:
            await interaction.response.send_message(NOT_ALLOWED_REPLY, ephemeral=True)
            return False
        if admin and not is_admin(bot, interaction.user.id):
            await interaction.response.send_message(NOT_ADMIN_REPLY, ephemeral=True)
            return False
        return True

    @tree.command(name="deep", description="讓大小姐認真思考回答一個問題（使用深度模型）")
    @app_commands.describe(question="你的問題")
    async def deep(interaction: discord.Interaction, question: str) -> None:
        if not await _guard(interaction, admin=False):
            return
        if not bot.chat.cooldown.try_acquire(interaction.user.id):
            await interaction.response.send_message("急什麼，等一下啦。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        name = getattr(interaction.user, "display_name", interaction.user.name)
        reply = await bot.chat.deep(interaction.channel_id, name, question)
        header = f"> {name}：{question[:200]}\n"
        chunks = split_message(header + "\n".join(reply.chunks))
        for chunk in chunks:
            await interaction.followup.send(chunk, allowed_mentions=bot.allowed_mentions)
        bot.schedule_summary(interaction.channel_id)

    @tree.command(name="reset", description="清除本頻道的對話歷史與摘要（管理員）")
    async def reset(interaction: discord.Interaction) -> None:
        if not await _guard(interaction, admin=True):
            return
        bot.history.reset(interaction.channel_id)
        await interaction.response.send_message("好啦，剛剛的事本小姐全忘了……才、才不是因為你叫我忘的！",
                                                ephemeral=True)

    @tree.command(name="persona_reload", description="重新讀取 persona.md（管理員）")
    async def persona_reload(interaction: discord.Interaction) -> None:
        if not await _guard(interaction, admin=True):
            return
        try:
            text = bot.persona.reload()
        except (OSError, ValueError) as exc:
            log.exception("persona 重新載入失敗")
            await interaction.response.send_message(f"讀取失敗：{type(exc).__name__}: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"已重新載入人設（{len(text)} 字）。", ephemeral=True)

    @tree.command(name="usage", description="顯示今日 token 用量與預估費用（管理員）")
    async def usage(interaction: discord.Interaction) -> None:
        if not await _guard(interaction, admin=True):
            return
        await interaction.response.send_message(format_usage(bot), ephemeral=True)
