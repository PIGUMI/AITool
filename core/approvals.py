"""R2操作の承認管理。

- 承認要求は「ツール名・引数・要約」を持ち、(ツール名, 引数) のハッシュで操作と結び付ける
- 承認はその要求にだけ有効で、1回しか実行されない（同時に2端末が押しても1回）
- 有効期限（既定10分）を過ぎた要求は無効化する
- 要求・承認・却下・期限切れ・実行結果はすべて監査ログに記録する
承認されると、保存してある (ツール, 引数) をそのまま実行する。モデルは承認後に引数を差し替えられない。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable

from policy.levels import Level
from policy.registry import ToolRegistry

from .audit import AuditLog

DEFAULT_TTL = 600.0
MAX_KEPT = 200


def operation_hash(tool: str, args: dict[str, Any]) -> str:
    """操作（ツール名＋引数）のハッシュ。キー順に依存しない。"""
    return hashlib.sha256(json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ApprovalManager:
    """承認待ちの操作を保持し、承認されたときに1回だけ実行する。"""

    def __init__(self, audit: AuditLog, registry: ToolRegistry, ttl: float = DEFAULT_TTL,
                 clock: Callable[[], float] = time.time) -> None:
        self.audit, self.registry, self.ttl, self.clock = audit, registry, ttl, clock
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # --- 内部 ---
    def _expire_locked(self, item: dict[str, Any]) -> None:
        if item["status"] == "pending" and self.clock() >= item["expires_at"]:
            item["status"] = "expired"
            self.audit.write("approval_expired", item["request_id"], approval_id=item["id"], tool=item["tool"], op_hash=item["hash"])

    @staticmethod
    def _view(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k != "_args_json"}

    # --- 要求 ---
    def request(self, request_id: str, tool: str, args: dict[str, Any], summary: str = "") -> dict[str, Any]:
        """承認要求を作る。同じ操作の未処理の要求があればそれを返す（重複を作らない）。"""
        h = operation_hash(tool, args)
        with self._lock:
            for it in self._items.values():
                self._expire_locked(it)
                if it["hash"] == h and it["status"] == "pending":
                    return self._view(it)
            now = self.clock()
            item = {"id": uuid.uuid4().hex[:12], "tool": tool, "args": json.loads(json.dumps(args)), "summary": summary or f"{tool} を実行します",
                    "hash": h, "status": "pending", "request_id": request_id, "created_at": now, "expires_at": now + self.ttl,
                    "decided_by": None, "result_summary": None}
            self._items[item["id"]] = item
            while len(self._items) > MAX_KEPT:
                self._items.pop(next(iter(self._items)))
            self.audit.write("approval_requested", request_id, approval_id=item["id"], tool=tool, args=args, op_hash=h,
                             expires_at=item["expires_at"], summary=item["summary"])
            return self._view(item)

    def list(self) -> list[dict[str, Any]]:
        """新しい順。期限切れはここで確定する。"""
        with self._lock:
            for it in self._items.values():
                self._expire_locked(it)
            return [self._view(it) for it in reversed(self._items.values())]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            it = self._items.get(approval_id)
            if it is None:
                return None
            self._expire_locked(it)
            return self._view(it)

    def pending_count(self) -> int:
        return sum(1 for a in self.list() if a["status"] == "pending")

    # --- 承認・却下 ---
    def approve(self, approval_id: str, shown_hash: str, device: str) -> dict[str, Any]:
        """承認して、保存してある操作を1回だけ実行する。shown_hash は、ユーザーが画面で見たハッシュ。"""
        with self._lock:
            it = self._items.get(approval_id)
            if it is None:
                return {"ok": False, "error": "not_found"}
            self._expire_locked(it)
            if it["status"] != "pending":  # 実行中・実行済み・却下・期限切れは再承認できない（流用・二重実行の防止）
                return {"ok": False, "error": f"not_pending:{it['status']}"}
            if shown_hash != it["hash"] or operation_hash(it["tool"], it["args"]) != it["hash"]:
                self.audit.write("approval_hash_mismatch", it["request_id"], approval_id=it["id"], device=device)
                return {"ok": False, "error": "hash_mismatch"}
            spec = self.registry.get(it["tool"])
            if spec is None or spec.level is not Level.R2:
                return {"ok": False, "error": "tool_not_available"}
            it["status"], it["decided_by"] = "executing", device
            self.audit.write("approval_granted", it["request_id"], approval_id=it["id"], tool=it["tool"], op_hash=it["hash"], device=device)
            args = json.loads(json.dumps(it["args"]))
        try:  # 実行はロックの外（長い操作が他の承認を止めない）。状態は executing なので他端末は実行できない
            out = spec.handler(args)
            status, result = "executed", out
        except Exception as e:
            status, result = "failed", {"error": f"{type(e).__name__}: {e}"}
        summary = json.dumps(result, ensure_ascii=False, default=str)[:300]
        with self._lock:
            it["status"], it["result_summary"] = status, summary
        self.audit.write("approved_execution", it["request_id"], approval_id=it["id"], tool=it["tool"], level="R2", args=args,
                         status=status, result_summary=summary, device=device)
        return {"ok": status == "executed", "status": status, "result": result}

    def reject(self, approval_id: str, device: str) -> dict[str, Any]:
        with self._lock:
            it = self._items.get(approval_id)
            if it is None:
                return {"ok": False, "error": "not_found"}
            self._expire_locked(it)
            if it["status"] != "pending":
                return {"ok": False, "error": f"not_pending:{it['status']}"}
            it["status"], it["decided_by"] = "rejected", device
            self.audit.write("approval_rejected", it["request_id"], approval_id=it["id"], tool=it["tool"], op_hash=it["hash"], device=device)
            return {"ok": True, "status": "rejected"}
