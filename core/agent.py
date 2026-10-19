"""エージェントループ（自前の薄い実装）。権限判定・検証・監査を、ツール呼び出しの入口1か所に集める。"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from policy.levels import Disposition, decide
from policy.registry import ToolRegistry

from .audit import AuditLog
from .llm import LLMClient, LLMError
from .prompts import SYSTEM_PROMPT
from .schema import validate
from .slots import SlotPool

MAX_RESULT_CHARS = 8000
EventCallback = Callable[[dict[str, Any]], None]


@dataclass
class AgentResult:
    request_id: str
    answer: str
    tool_calls: int = 0
    stopped_reason: str = "done"  # done / tool_limit / turn_limit / llm_error
    trace: list[dict[str, Any]] = field(default_factory=list)


def _wrap(name: str, payload: Any) -> str:
    """ツールの戻り値を「データ」として包む。"""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "…(省略)"
    return f'<tool_result name="{name}">{text}</tool_result>'


class Agent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, audit: AuditLog,
                 max_tool_calls: int = 20, max_llm_turns: int = 25,
                 slots: SlotPool | None = None) -> None:
        self.llm, self.registry, self.audit = llm, registry, audit
        self.max_tool_calls, self.max_llm_turns = max_tool_calls, max_llm_turns
        self.slots = slots or SlotPool()

    def run(self, request: str, slot: str = "interactive", on_event: EventCallback | None = None) -> AgentResult:
        """1件の依頼を処理する。"""
        rid = uuid.uuid4().hex[:12]
        emit = on_event or (lambda e: None)
        self.audit.write("request", rid, request=request[:200], slot=slot)
        with self.slots.acquire(slot):
            result = self._loop(rid, request, emit)
        self.audit.write("response", rid, request=request[:200], stopped_reason=result.stopped_reason,
                         tool_calls=result.tool_calls, result_summary=result.answer[:200])
        return result

    def _loop(self, rid: str, request: str, emit: EventCallback) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request},
        ]
        res = AgentResult(request_id=rid, answer="")
        tools = self.registry.openai_tools()
        for _ in range(self.max_llm_turns):
            try:
                msg = self.llm.chat(messages, tools)
            except LLMError as e:
                res.stopped_reason, res.answer = "llm_error", f"モデルに接続できませんでした: {e}"
                return res
            calls = msg.get("tool_calls") or []
            if not calls:
                res.answer = msg.get("content") or ""
                emit({"type": "message", "text": res.answer})
                return res
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                if res.tool_calls >= self.max_tool_calls:
                    res.stopped_reason = "tool_limit"
                    res.answer = (f"ツール呼び出しの上限（{self.max_tool_calls}回）に達したため停止しました。"
                                  "ここまでに実行した内容は監査ログに記録されています。")
                    self.audit.write("limit_reached", rid, limit=self.max_tool_calls)
                    emit({"type": "message", "text": res.answer})
                    return res
                res.tool_calls += 1
                content = self._execute(rid, request, call, res, emit)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})
        res.stopped_reason = "turn_limit"
        res.answer = f"モデルへの問い合わせ回数の上限（{self.max_llm_turns}回）に達したため停止しました。"
        self.audit.write("limit_reached", rid, limit=self.max_llm_turns, kind="llm_turns")
        emit({"type": "message", "text": res.answer})
        return res

    def _execute(self, rid: str, request: str, call: dict[str, Any], res: AgentResult, emit: EventCallback) -> str:
        fn = call.get("function", {})
        name = fn.get("name", "")
        spec = self.registry.get(name)

        def finish(status: str, payload: Any, level: str | None = None, args: Any = None) -> str:
            self.audit.write("tool_call", rid, request=request[:200], tool=name, level=level,
                             args=args, status=status, result_summary=json.dumps(payload, ensure_ascii=False, default=str)[:300])
            res.trace.append({"tool": name, "status": status})
            emit({"type": "tool", "tool": name, "status": status})
            return _wrap(name, payload)

        if spec is None:
            return finish("unknown_tool", {"error": f"未登録のツールです: {name}"})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            return finish("bad_arguments", {"error": "引数がJSONではありません"}, spec.level.name)
        errors = validate(args, spec.input_schema)
        if errors:
            return finish("invalid_arguments", {"error": errors}, spec.level.name, args)
        disp = decide(spec.level)
        if disp is Disposition.DENIED:
            return finish("denied", {"error": "このツールは実行できません"}, spec.level.name, args)
        if disp is Disposition.REQUIRES_APPROVAL:
            # R2の承認フローはフェーズ3。それまでは実行しない。
            return finish("approval_required", {"executed": False, "message": "承認が必要な操作です。承認機能は未実装のため実行していません。"},
                          spec.level.name, args)
        try:
            out = spec.handler(args)
        except Exception as e:  # ツールの失敗はモデルに返し、ループは続ける
            return finish("error", {"error": f"{type(e).__name__}: {e}"}, spec.level.name, args)
        return finish("ok", out, spec.level.name, args)
