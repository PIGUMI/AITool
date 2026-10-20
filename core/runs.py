"""作業(run)の追跡。実行中・直近の依頼の状態を持ち、管理画面から見たり中止したりできるようにする。

履歴の正は監査ログ。ここは実行中の状態と直近の経過を持つだけで、再起動すると消える。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

MAX_RUNS = 200
MAX_EVENTS = 100


class RunTracker:
    """実行中・直近の run を保持する。スレッドセーフ。"""

    def __init__(self, max_runs: int = MAX_RUNS) -> None:
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cancel: set[str] = set()
        self._lock = threading.Lock()
        self._max = max_runs

    def start(self, rid: str, request: str, slot: str, device: str = "-") -> None:
        with self._lock:
            self._runs[rid] = {"request_id": rid, "request": request[:200], "slot": slot, "device": device,
                               "status": "running", "started_at": time.time(), "finished_at": None,
                               "tool_calls": 0, "events": [], "answer": "", "stopped_reason": None}
            while len(self._runs) > self._max:
                self._runs.popitem(last=False)

    def event(self, rid: str, ev: dict[str, Any]) -> None:
        with self._lock:
            r = self._runs.get(rid)
            if r is None:
                return
            if ev.get("type") == "tool":
                r["tool_calls"] += 1
            if len(r["events"]) < MAX_EVENTS:
                r["events"].append({"t": round(time.time() - r["started_at"], 2), **ev})

    def finish(self, rid: str, stopped_reason: str, answer: str, tool_calls: int) -> None:
        with self._lock:
            r = self._runs.get(rid)
            if r is None:
                return
            r.update(status="done" if stopped_reason == "done" else stopped_reason, stopped_reason=stopped_reason,
                     answer=answer[:2000], tool_calls=tool_calls, finished_at=time.time())
            self._cancel.discard(rid)

    def cancel(self, rid: str) -> bool:
        """実行中の run に中止を要求する。次のモデル問い合わせ/ツール実行の前に止まる。"""
        with self._lock:
            r = self._runs.get(rid)
            if r is None or r["status"] != "running":
                return False
            self._cancel.add(rid)
            return True

    def is_cancelled(self, rid: str) -> bool:
        with self._lock:
            return rid in self._cancel

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._runs.get(rid)
            return {**r, "events": list(r["events"])} if r else None

    def list(self) -> list[dict[str, Any]]:
        """新しい順。events は含めない。"""
        with self._lock:
            return [{k: v for k, v in r.items() if k != "events"} for r in reversed(self._runs.values())]
