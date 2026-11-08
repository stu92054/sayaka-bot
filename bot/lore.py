"""原作資料（lore/*.md）載入。

整份常駐在 system prompt 的最前面（persona 之前），內容固定不變，
所以 DeepSeek 的前綴快取幾乎每次都會命中，費用很低。
說明文字只寫用法，不帶角色名稱；角色相關內容全在 lore/ 的檔案裡。
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

LORE_INSTRUCTIONS = (
    "以下 <lore> 標籤內是你所扮演角色的原作資料（官方設定、人物關係與劇情）。\n"
    "- 只在有人問到原作的人物、劇情、設定時參考，並用角色自己的記憶與口吻回答。\n"
    "- 不要主動背誦或整段引用，也不要在與原作無關的對話中提起。\n"
    "- 資料裡沒有的事就說不確定，不要編造。\n"
    "- 回答時不要提到「資料」「lore」或標籤這類字眼。"
)


def common_dir_for(directory: Path) -> Path:
    """所有角色共用的原作資料：characters/_common/lore（角色的 lore 是 characters/<key>/lore）。"""
    return directory.parent.parent / "_common" / "lore"


class Lore:
    def __init__(self, directory: str | Path | None, common: str | Path | None = None):
        """common：共用資料夾，預設是 common_dir_for(directory)；不存在就只讀角色自己的。
        共用的檔案排在最前面（每個角色都一樣的前綴，之後也方便跨角色命中快取）。"""
        self.directory = Path(directory) if directory is not None else None
        self.common = Path(common) if common is not None else (
            common_dir_for(self.directory) if self.directory is not None else None)
        self.text = ""
        self.files: list[str] = []
        self.warnings: list[str] = []
        if self.directory is not None:
            self.reload()

    @property
    def enabled(self) -> bool:
        return bool(self.text)

    def reload(self) -> list[str]:
        """重新讀取資料夾內的 *.md（依檔名排序）。回傳警告訊息。

        資料夾不存在或沒有內容時：若先前已載入過，保留舊內容；否則不使用原作資料。
        """
        warnings: list[str] = []
        if self.directory is None:
            self.warnings = []
            return []
        parts: list[str] = []
        names: list[str] = []
        if not self.directory.is_dir():
            warnings.append(f"找不到原作資料夾 {self.directory}")
        else:
            sources = [(p, f"共通_{p.name}") for p in sorted(self.common.glob("*.md"))] \
                if self.common is not None and self.common.is_dir() else []
            sources += [(p, p.name) for p in sorted(self.directory.glob("*.md"))]
            for path, name in sources:
                try:
                    content = path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError) as exc:
                    warnings.append(f"{name} 讀取失敗（{type(exc).__name__}），已略過")
                    continue
                if not content:
                    warnings.append(f"{name} 是空的，已略過")
                    continue
                parts.append(f'<file name="{name}">\n{content}\n</file>')
                names.append(name)
            if not parts:
                warnings.append(f"{self.directory} 裡沒有可用的 .md 檔")

        if parts:
            self.text = f"{LORE_INSTRUCTIONS}\n\n<lore>\n" + "\n\n".join(parts) + "\n</lore>"
            self.files = names
            log.info("已載入原作資料 %d 個檔案（%d 字）", len(names), len(self.text))
        elif self.text:
            warnings.append("保留目前的原作資料")
        else:
            warnings.append("不使用原作資料")
        for w in warnings:
            log.warning(w)
        self.warnings = warnings
        return warnings
