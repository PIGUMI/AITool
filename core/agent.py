"""エージェントループ（自前の薄い実装）。権限判定・検証・監査を、ツール呼び出しの入口1か所に集める。"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from policy.levels import Disposition, decide
from policy.registry import ToolRegistry

from .audit import AuditLog
from .llm import LLMClient, LLMError
from .prompts import SYSTEM_PROMPT
from .runs import RunTracker
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


_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)
RETRY_PROMPT = "ツールの結果に基づいて、最終回答を日本語で簡潔に述べてください。"


def _final_text(msg: dict[str, Any]) -> str:
    """本文を取り出す。content が null・空、または思考タグだけの場合は空文字を返す。"""
    return _THINK.sub("", msg.get("content") or "").strip()


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
        self.tracker = RunTracker()

    def run(self, request: str, slot: str = "interactive", on_event: EventCallback | None = None,
            device: str = "-") -> AgentResult:
        """1件の依頼を処理する。経過は tracker に残り、管理画面から中止できる。"""
        rid = uuid.uuid4().hex[:12]
        user_emit = on_event or (lambda e: None)

        def emit(ev: dict[str, Any]) -> None:
            self.tracker.event(rid, ev)
            user_emit(ev)

        self.tracker.start(rid, request, slot, device)
        self.audit.write("request", rid, request=request[:200], slot=slot, device=device)
        try:
            with self.slots.acquire(slot):
                result = self._loop(rid, request, emit)
        except BaseException:
            self.tracker.finish(rid, "error", "", 0)
            raise
        self.tracker.finish(rid, result.stopped_reason, result.answer, result.tool_calls)
        self.audit.write("response", rid, request=request[:200], stopped_reason=result.stopped_reason,
                         tool_calls=result.tool_calls, result_summary=result.answer[:200])
        return result

    def _cancelled(self, rid: str, res: AgentResult, emit: EventCallback) -> bool:
        """中止が要求されていれば結果を整えて True を返す。"""
        if not self.tracker.is_cancelled(rid):
            return False
        res.stopped_reason, res.answer = "cancelled", "管理画面からの要求で作業を中止しました。ここまでの実行内容は監査ログに記録されています。"
        self.audit.write("cancelled", rid, tool_calls=res.tool_calls)
        emit({"type": "message", "text": res.answer})
        return True

    def _loop(self, rid: str, request: str, emit: EventCallback) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request},
        ]
        res = AgentResult(request_id=rid, answer="")
        tools = self.registry.openai_tools()
        retried = False
        for _ in range(self.max_llm_turns):
            if self._cancelled(rid, res, emit):
                return res
            try:
                msg = self.llm.chat(messages, tools)
            except LLMError as e:
                res.stopped_reason, res.answer = "llm_error", f"モデルに接続できませんでした: {e}"
                return res
            calls = msg.get("tool_calls") or []
            if not calls:
                res.answer = _final_text(msg)
                if not res.answer:
                    # 空の最終回答：形だけを記録し（本文は記録しない）、1回だけ回答を促す
                    self.audit.write("empty_response", rid, message_keys=sorted(msg), finish_reason=msg.get("_finish_reason"),
                                     reasoning_chars=len(msg.get("reasoning_content") or ""), retried=retried)
                    if not retried:
                        retried = True
                        messages.append({"role": "user", "content": RETRY_PROMPT})
                        continue
                    res.stopped_reason = "empty_response"
                    res.answer = "モデルが空の応答を返しました。ツールは実行済みです。詳細は監査ログと診断（python -m core.diagnose）を確認してください。"
                emit({"type": "message", "text": res.answer})
                return res
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                if self._cancelled(rid, res, emit):
                    return res
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
