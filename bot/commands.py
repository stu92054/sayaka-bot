"""Slash commands：/deep、/reset、/persona_reload、/usage。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .text import split_message

if TYPE_CHECKING:
    from .main import PersonaBot

log = logging.getLogger(__name__)


def is_admin(bot: "PersonaBot", user_id: int) -> bool:
    return user_id in bot.config.admin_user_ids


def format_usage(bot: "PersonaBot") -> str:
    rows = bot.llm.usage_today()
    total = sum(r.total_tokens for r in rows)
    cost = bot.llm.estimate_cost(rows)
    cost_budget = bot.config.daily_cost_budget
    token_budget = bot.config.daily_token_budget
    lines = [f"**今日用量（{bot.llm.today()}）**"]
    cfg = bot.config
    lines.append(f"費用：約 {cfg.money(cost)} / 上限 {cfg.currency_symbol}{cost_budget:g}（{cost / cost_budget:.1%}）"
                 if cost_budget > 0 else f"費用：約 {cfg.money(cost)}（未設上限）")
    lines.append(f"總 tokens：{total:,} / 上限 {token_budget:,}（{total / token_budget:.1%}）"
                 if token_budget > 0 else f"總 tokens：{total:,}")
    for r in rows:
        if r.model in ("deepseek-search", "deepseek-fetch"):  # 按次計費，沒有 token
            label = "搜尋" if r.model == "deepseek-search" else "讀取網頁"
            lines.append(f"- {label}：{r.calls} 次，約 {bot.config.money(r.cost)}")
            continue
        hit_rate = r.cache_hit_tokens / r.prompt_tokens if r.prompt_tokens else 0
        lines.append(
            f"- `{r.model}`：{r.calls} 次，輸入 {r.prompt_tokens:,}（快取命中 {r.cache_hit_tokens:,}，{hit_rate:.0%}），"
            f"輸出 {r.completion_tokens:,}"
            + (f"（推理 {r.reasoning_tokens:,}）" if r.reasoning_tokens else "")
            + f"，約 {bot.config.money(r.cost)}"
        )
    lines.append(f"（費用依 DeepSeek {bot.config.price_currency} 價目表估算，已區分高峰／空閒時段）")
    lines.append("目前為 **高峰時段**（價格 ×1）" if bot.llm.is_peak()
                 else f"目前為空閒時段（價格 ×{bot.config.offpeak_ratio:g}）")
    cal = bot.llm.calendar
    if cal is not None:
        years = cal.loaded_years()
        lines.append(f"節假日資料：{', '.join(map(str, years)) if years else '尚未載入（只用 CN_HOLIDAYS）'}")
    return "\n".join(lines)


def reload_persona_and_replies(bot: "PersonaBot") -> str:
    """重新讀取 persona.md 與 replies.toml，回傳給管理員看的結果。"""
    lines = []
    try:
        bot.persona.reload()
        lines.append(f"已重新載入人設（{len(bot.persona.base)} 字）。")
    except (OSError, ValueError) as exc:
        log.exception("persona 重新載入失敗")
        lines.append(f"人設讀取失敗，沿用舊的人設：{type(exc).__name__}: {exc}")
    lore = getattr(bot, "lore", None)
    if lore is not None and lore.directory is not None:
        lore_warnings = lore.reload()
        if lore.enabled:
            lines.append(f"已重新載入原作資料（{len(lore.files)} 個檔案，{len(lore.text)} 字）。")
        lines.extend(f"- {w}" for w in lore_warnings[:5])
    memories = getattr(bot, "memories", None)
    if memories is not None and memories.directory is not None:
        memory_warnings = memories.reload()
        if memories.enabled:
            lines.append(f"已重新載入回憶卡（{len(memories.cards)} 張）。")
        lines.extend(f"- {w}" for w in memory_warnings[:5])
    warnings = bot.replies.reload()
    if warnings:
        lines.append("固定訊息已重新載入，但有以下警告：")
        lines.extend(f"- {w}" for w in warnings[:10])
    else:
        lines.append("已重新載入固定訊息。")
    lines.append("（指令說明要重啟 bot 才會更新。）")
    return "\n".join(lines)


def register_commands(bot: "PersonaBot") -> None:
    tree = bot.tree

    async def _guard(interaction: discord.Interaction, *, admin: bool) -> bool:
        guild = interaction.guild
        if guild is not None and guild.id not in bot.config.allowed_guild_ids:
            await interaction.response.send_message(bot.replies["not_allowed"], ephemeral=True)
            return False
        if admin and not is_admin(bot, interaction.user.id):
            await interaction.response.send_message(bot.replies["not_admin"], ephemeral=True)
            return False
        return True

    # 指令說明在註冊時讀取，Discord 同步後就固定了：修改 replies.toml 的 *_description 要重啟 bot
    @tree.command(name="deep", description=bot.replies["deep_description"])
    @app_commands.describe(question="你的問題", image="附上一張圖片（選填）")
    async def deep(interaction: discord.Interaction, question: str,
                   image: discord.Attachment | None = None) -> None:
        if not await _guard(interaction, admin=False):
            return
        if not bot.chat.cooldown.try_acquire(interaction.user.id):
            await interaction.response.send_message(bot.replies["cooldown"], ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        name = getattr(interaction.user, "display_name", interaction.user.name)
        reply = await bot.chat.deep(interaction.channel_id, name, question, [image] if image else None)
        header = f"> {name}：{question[:200]}\n"
        chunks = split_message(header + "\n".join(reply.chunks))
        # 回覆開頭會引用提問原文；問題裡若有 <@id>，不能讓 bot 替提問者 ping 人
        for chunk in chunks:
            await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        bot.schedule_summary(interaction.channel_id)

    @tree.command(name="reset", description=bot.replies["reset_description"])
    async def reset(interaction: discord.Interaction) -> None:
        if not await _guard(interaction, admin=True):
            return
        bot.history.reset(interaction.channel_id)
        await interaction.response.send_message(bot.replies["reset"], ephemeral=True)

    @tree.command(name="persona_reload", description=bot.replies["persona_reload_description"])
    async def persona_reload(interaction: discord.Interaction) -> None:
        if not await _guard(interaction, admin=True):
            return
        await interaction.response.send_message(reload_persona_and_replies(bot), ephemeral=True)

    @tree.command(name="usage", description=bot.replies["usage_description"])
    async def usage(interaction: discord.Interaction) -> None:
        if not await _guard(interaction, admin=True):
            return
        await interaction.response.send_message(format_usage(bot), ephemeral=True)
