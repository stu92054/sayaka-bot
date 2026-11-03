"""文字處理工具。"""
from __future__ import annotations

DISCORD_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """把超過 limit 字元的回覆分段，優先在換行、其次在空白處切開。"""
    if limit <= 0:
        raise ValueError("limit must be positive")
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            space = window.rfind(" ")
            cut = space if space >= limit // 2 else cut
        if cut <= 0:
            cut = limit
        chunk = text[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
