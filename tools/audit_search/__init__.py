"""audit_search（R0）：このアシスタント自身の監査ログを検索する（「さっき何を実行した？」への根拠）。"""
from __future__ import annotations

from typing import Any

from core.audit import AuditLog
from policy.levels import Level
from policy.registry import ToolSpec


def build_tool(audit: AuditLog) -> ToolSpec:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "event": {"type": "string", "enum": ["request", "response", "tool_call", "auth_failed", "limit_reached", "empty_response", "cancelled"]},
            "tool": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 30},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "additionalProperties": False,
    }

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        ev = audit.search(int(args.get("days", 1)), args.get("text", ""), args.get("event", ""), args.get("tool", ""), "",
                          int(args.get("limit", 10)))
        return {"events": ev, "note": "新しい順。ts は JST。"}

    return ToolSpec("audit_search", "このアシスタントの実行履歴（監査ログ）を検索する。いつ・何を・どのツールで実行したかの根拠になる。", schema, Level.R0, handler)
