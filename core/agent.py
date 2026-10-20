"""エージェントループ（自前の薄い実装）。権限判定・検証・監査を、ツール呼び出しの入口1か所に集める。"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from policy.levels import Disposition, decide
from policy.registry import ToolRegistry

from .approvals import ApprovalManager
from .audit import AuditLog
from .bridge import DeviceBridge
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
    tainted: bool = False  # 端末のファイルなど私的データを読んだ。以降、外部へ通信するツールは使えない（情報の持ち出し防止）


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
                 slots: SlotPool | None = None, force_rules: list[dict[str, Any]] | None = None,
                 approval_ttl: float = 600.0, bridge: DeviceBridge | None = None, updater: Any = None) -> None:
        # force_rules: [{"pattern": 正規表現, "tools": [ツール名...]}]。依頼が一致したら、最初の1回はそのツールの呼び出しを強制する
        self.force_rules = [(re.compile(r["pattern"]), list(r["tools"])) for r in (force_rules or [])]
        self.llm, self.registry, self.audit = llm, registry, audit
        self.max_tool_calls, self.max_llm_turns = max_tool_calls, max_llm_turns
        self.slots = slots or SlotPool()
        self.tracker = RunTracker()
        self.approvals = ApprovalManager(audit, registry, ttl=approval_ttl)
        self.bridge = bridge or DeviceBridge()  # アプリが動いている端末との橋渡し（端末のファイル操作）
        self.updater = updater                  # 自己更新（[update] remote_url が設定されているときだけ）

    def run(self, request: str, slot: str = "interactive", on_event: EventCallback | None = None,
            device: str = "-", request_id: str | None = None, history: list[dict[str, Any]] | None = None,
            tainted: bool = False) -> AgentResult:
        """1件の依頼を処理する。経過は tracker に残り、管理画面から中止できる。

        history: 同じ会話の直前までのやり取り（{"role": "user"|"assistant", "content": ...}）。短い返事（「1」など）の文脈になる。
        tainted: 同じ会話の中で、すでに私的データ（端末のファイル）を読んでいる。外部通信ツールは、この会話では使えない。
        """
        rid = request_id or uuid.uuid4().hex[:12]
        user_emit = on_event or (lambda e: None)

        def emit(ev: dict[str, Any]) -> None:
            self.tracker.event(rid, ev)
            user_emit(ev)

        self.tracker.start(rid, request, slot, device)
        self.audit.write("request", rid, request=request[:200], slot=slot, device=device)
        try:
            with self.slots.acquire(slot):
                result = self._loop(rid, request, emit, history or [], tainted)
        except BaseException:
            self.tracker.finish(rid, "error", "", 0)
            raise
        self.tracker.finish(rid, result.stopped_reason, result.answer, result.tool_calls)
        self.audit.write("response", rid, request=request[:200], stopped_reason=result.stopped_reason,
                         tool_calls=result.tool_calls, result_summary=result.answer[:200])
        return result

    def _forced_tools(self, request: str) -> set[str]:
        """依頼に一致するルールのツール名（登録済みのものだけ）。無ければ空。"""
        names: set[str] = set()
        for pattern, tools in self.force_rules:
            if pattern.search(request):
                names.update(t for t in tools if self.registry.get(t))
        return names

    def _cancelled(self, rid: str, res: AgentResult, emit: EventCallback) -> bool:
        """中止が要求されていれば結果を整えて True を返す。"""
        if not self.tracker.is_cancelled(rid):
            return False
        res.stopped_reason, res.answer = "cancelled", "管理画面からの要求で作業を中止しました。ここまでの実行内容は監査ログに記録されています。"
        self.audit.write("cancelled", rid, tool_calls=res.tool_calls)
        emit({"type": "message", "text": res.answer})
        return True

    def _loop(self, rid: str, request: str, emit: EventCallback, history: list[dict[str, Any]] | None = None,
              tainted: bool = False) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": m["role"], "content": m["content"]} for m in (history or []) if m.get("role") in ("user", "assistant")]
        messages.append({"role": "user", "content": request})
        res = AgentResult(request_id=rid, answer="", tainted=tainted)
        tools = self.registry.openai_tools()
        retried = False
        forced = self._forced_tools(request)
        if forced:
            self.audit.write("forced_tools", rid, tools=sorted(forced))
        for turn in range(self.max_llm_turns):
            if self._cancelled(rid, res, emit):
                return res
            try:
                if turn == 0 and forced:  # 最初の1回だけ、該当ツールの呼び出しを強制する（記憶で答えさせない）
                    msg = self.llm.chat(messages, [t for t in tools if t["function"]["name"] in forced], tool_choice="required")
                else:
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
            # 端末のファイルなど私的データの内容は、監査ログに残さない（引数と状態だけ記録する）
            summary = "(私的データのため内容は記録しません)" if spec is not None and spec.reads_private and status == "ok" \
                else json.dumps(payload, ensure_ascii=False, default=str)[:300]
            self.audit.write("tool_call", rid, request=request[:200], tool=name, level=level,
                             args=args, status=status, result_summary=summary)
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
        if spec.egress and res.tainted:
            # 私的データを読んだ依頼では、外部へ通信するツールを止める。
            # （Web ページや読んだファイルに仕込まれた指示で、内容が URL・検索語・通知に載って出ていくのを防ぐ）
            return finish("egress_blocked", {"error": "この依頼では端末のファイルなどの私的データを読んだため、外部へ通信するツールは使えません。"
                                                     "外部の情報が必要なら、別の依頼として行ってください。"}, spec.level.name, args)
        disp = decide(spec.level)
        if disp is Disposition.DENIED:
            return finish("denied", {"error": "このツールは実行できません"}, spec.level.name, args)
        if disp is Disposition.REQUIRES_APPROVAL:
            # R2は実行しない。承認要求を作り、ユーザーが承認画面で承認したときに、保存した操作が1回だけ実行される。
            summary = spec.summarizer(args) if spec.summarizer else ""
            ap = self.approvals.request(rid, name, args, summary)
            emit({"type": "approval", "approval_id": ap["id"], "tool": name, "summary": ap["summary"]})
            return finish("approval_required", {"executed": False, "approval_id": ap["id"], "expires_in_seconds": int(self.approvals.ttl),
                                                "message": "承認が必要な操作です。まだ実行していません。ユーザーが管理画面の「承認」で許可すると実行されます。"
                                                           "実行したと言ってはいけません。"}, spec.level.name, args)
        try:
            out = spec.handler(args)
        except Exception as e:  # ツールの失敗はモデルに返し、ループは続ける
            return finish("error", {"error": f"{type(e).__name__}: {e}"}, spec.level.name, args)
        if spec.reads_private and not (isinstance(out, dict) and out.get("error")):
            res.tainted = True
            self.audit.write("tainted", rid, tool=name)
        return finish("ok", out, spec.level.name, args)
