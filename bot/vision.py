"""圖片附件：篩選、下載、轉成 DeepSeek 的 image_url 區塊。

只處理觸發回覆的那一則訊息的圖片；歷史紀錄裡只留 [圖片:檔名]，避免每次都重送圖片、費用累積。
圖片由 bot 下載後以 base64 送出：DeepSeek 的伺服器連不到 Discord 的 CDN。
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

# 支援看圖的模型（官方價格頁：deepseek-flash 支援，deepseek-v4-pro 不支援；
# 送圖片給不支援的模型不會報錯，圖片會被默默丟掉，模型會說看不到）
VISION_MODELS = frozenset({"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"})

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 依檔案開頭判斷格式（DeepSeek 也是看內容而不是副檔名）
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def is_image(attachment: Any) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    if content_type.startswith("image/"):
        return True
    return (getattr(attachment, "filename", "") or "").lower().endswith(IMAGE_EXTENSIONS)


def image_mime(data: bytes) -> str | None:
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def supports_vision(model: str, config: Config) -> bool:
    return config.vision_enabled and model in VISION_MODELS


async def load_images(attachments: list[Any], config: Config) -> list[str]:
    """下載圖片附件，回傳 data URL。太大、讀取失敗或不是支援的格式就略過（只記 log）。"""
    urls: list[str] = []
    for a in [a for a in attachments if is_image(a)][: config.vision_max_images]:
        name = getattr(a, "filename", "圖片")
        size = getattr(a, "size", 0) or 0
        if size > config.vision_max_bytes:
            log.info("圖片 %s 太大（%d bytes），不送給模型", name, size)
            continue
        try:
            data = await a.read()
        except Exception as exc:  # noqa: BLE001 - 下載失敗就當作沒有這張圖
            log.warning("下載圖片 %s 失敗：%s", name, type(exc).__name__)
            continue
        mime = image_mime(data)
        if mime is None or len(data) > config.vision_max_bytes:
            log.info("圖片 %s 格式不支援或太大，不送給模型", name)
            continue
        urls.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
    return urls


def attach_images(prompt: list[dict[str, Any]], image_urls: list[str], detail: str) -> list[dict[str, Any]]:
    """把圖片加到 prompt 最後一則（user）訊息。圖片只能放在 user 訊息裡。"""
    if not image_urls or not prompt or prompt[-1]["role"] != "user":
        return prompt
    last = prompt[-1]
    blocks: list[dict[str, Any]] = [{"type": "text", "text": last["content"]}]
    blocks += [{"type": "image_url", "image_url": {"url": u, "detail": detail}} for u in image_urls]
    return [*prompt[:-1], {"role": "user", "content": blocks}]
