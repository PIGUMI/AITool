"""ツール登録簿。権限レベルのないツール、R3のツールは登録できない。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .levels import Level, PolicyError


@dataclass(frozen=True)
class ToolSpec:
    """ツールの定義（入力スキーマ・権限レベル・実体）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    level: Level | None
    handler: Callable[[dict[str, Any]], Any]
    summarizer: Callable[[dict[str, Any]], str] | None = None  # R2: 承認画面に出す「何をするか」の要約


class ToolRegistry:
    """権限レベル付きのツールだけを保持する。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """登録する。権限レベルなし・R3・重複・不正スキーマは PolicyError。"""
        if not isinstance(spec.level, Level):
            raise PolicyError(f"{spec.name}: 権限レベルがないツールは登録できません")
        if spec.level is Level.R3:
            raise PolicyError(f"{spec.name}: R3（禁止）のツールは登録できません")
        if spec.name in self._tools:
            raise PolicyError(f"{spec.name}: すでに登録されています")
        if spec.input_schema.get("type") != "object":
            raise PolicyError(f"{spec.name}: 入力スキーマは object 型にしてください")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        """OpenAI互換の tools 定義に変換する。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": f"[{t.level.name}] {t.description}",
                    "parameters": t.input_schema,
                },
            }
            for t in self._tools.values()
        ]
