"""persona.md 載入。人設內容只存在於 persona.md，程式不寫死。"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Persona:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.text = ""
        self.reload()

    def reload(self) -> str:
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"{self.path} 是空的")
        self.text = text
        log.info("已載入人設 %s（%d 字）", self.path, len(text))
        return text
