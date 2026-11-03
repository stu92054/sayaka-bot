"""固定訊息與指令說明，從 replies.toml 載入。

角色口吻的台詞只放在 replies.toml，程式內只保留中性的預設值：
檔案不存在、格式錯誤或缺項時使用預設值並記錄 warning，不會讓 bot 起不來。
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path

log = logging.getLogger(__name__)

# 中性預設值（不帶任何角色口吻）
DEFAULT_REPLIES: dict[str, str] = {
    # 一般回覆
    "budget_exceeded": "今天的使用額度已經用完了，請明天再試。",
    "error": "目前無法回應，請稍後再試。",
    "content_filter": "這個內容無法回應。",
    # slash commands
    "not_admin": "這個指令只有管理員可以使用。",
    "not_allowed": "這個伺服器沒有開放使用。",
    "cooldown": "請稍候再試。",
    "reset": "已清除本頻道的對話歷史與摘要。",
    # slash command 說明（Discord 同步時固定，修改後要重啟 bot 才會更新）
    "deep_description": "用深度模型認真回答一個問題",
    "reset_description": "清除本頻道的對話歷史與摘要（管理員）",
    "persona_reload_description": "重新讀取人設、原作資料與固定訊息（管理員）",
    "usage_description": "顯示今日 token 用量與預估費用（管理員）",
}

# Discord 對 slash command 說明的長度上限
DESCRIPTION_MAX_LEN = 100


class Replies:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._values: dict[str, str] = dict(DEFAULT_REPLIES)
        self._file_loaded = False
        self.warnings: list[str] = []
        if self.path is not None:
            self.reload()

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __getattr__(self, key: str) -> str:
        values = self.__dict__.get("_values", {})
        if key in values:
            return values[key]
        raise AttributeError(key)

    def reload(self) -> list[str]:
        """重新讀檔，回傳這次載入的警告訊息（也會寫進 log）。

        讀檔或解析失敗時：若先前已成功載入過，保留舊內容；否則使用預設值。
        """
        warnings: list[str] = []
        data: dict | None = None
        if self.path is None:
            warnings.append("未設定 replies.toml 路徑，使用預設訊息")
        else:
            try:
                with self.path.open("rb") as f:
                    data = tomllib.load(f)
            except FileNotFoundError:
                warnings.append(f"找不到 {self.path}，使用預設訊息")
            except (OSError, tomllib.TOMLDecodeError) as exc:
                keep = "保留目前的訊息" if self._file_loaded else "使用預設訊息"
                warnings.append(f"{self.path} 讀取失敗（{type(exc).__name__}: {exc}），{keep}")
                self._finish(warnings)
                return warnings

        values = dict(DEFAULT_REPLIES)
        if data is not None:
            for key in DEFAULT_REPLIES:
                if key not in data:
                    warnings.append(f"replies.toml 缺少 {key}，使用預設值")
                    continue
                value = data[key]
                if not isinstance(value, str) or not value.strip():
                    warnings.append(f"replies.toml 的 {key} 必須是非空字串，使用預設值")
                    continue
                value = value.strip()
                if key.endswith("_description") and len(value) > DESCRIPTION_MAX_LEN:
                    warnings.append(f"replies.toml 的 {key} 超過 Discord 上限 {DESCRIPTION_MAX_LEN} 字，已截斷")
                    value = value[:DESCRIPTION_MAX_LEN]
                values[key] = value
            for key in sorted(set(data) - set(DEFAULT_REPLIES)):
                warnings.append(f"replies.toml 有未知的項目 {key}，已忽略")
            self._file_loaded = True
        self._values = values
        self._finish(warnings)
        return warnings

    def _finish(self, warnings: list[str]) -> None:
        self.warnings = warnings
        for w in warnings:
            log.warning(w)
        if self.path is not None and not warnings:
            log.info("已載入 %s", self.path)
