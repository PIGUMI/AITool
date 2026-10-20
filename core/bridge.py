"""端末ブリッジ。アプリ（Windows EXE / Android）が動いている端末のファイルを、サーバーの Faraway から使えるようにする。

仕組み: アプリがサーバーへ「ロングポーリング」で接続し（端末→サーバーの発信のみ。端末側の待ち受けポートは不要）、
サーバーが出した操作（list / read / write）を端末が実行して結果を返す。

安全の分担:
- 端末（アプリ）が権威: 利用者がアプリで許可したフォルダ（ルート）の中だけ。ルートごとに読み取り専用／読み書きを選ぶ。
  サーバー側のバグや乗っ取りがあっても、許可フォルダの外・秘匿ファイルには触れない（アプリ側でも拒否する）。
- サーバー: 権限レベル（list/read=R0、write=R2＝承認必須）、引数の検証、監査ログ。
- 端末のファイルを読んだ依頼では、外部へ通信するツールを止める（Agent の tainted）。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

OFFLINE_AFTER = 45.0      # ポーリングが途絶えてからオフライン扱いにするまでの秒数
MAX_QUEUE = 20
MAX_RESULT_BYTES = 600_000
OPS = ("list", "read", "write")


class DeviceBridge:
    """接続中の端末と、操作の受け渡し。スレッドセーフ。"""

    def __init__(self, clock: Callable[[], float] = time.time, offline_after: float = OFFLINE_AFTER) -> None:
        self.clock, self.offline_after = clock, offline_after
        self._cv = threading.Condition()
        self._info: dict[str, dict[str, Any]] = {}
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    # --- 端末側（API 経由）---
    def hello(self, device: str, info: dict[str, Any]) -> None:
        """端末が接続時に、許可フォルダ（名前とモードだけ。実パスは送らせない）とプラットフォームを申告する。"""
        roots = [{"name": str(r.get("name", ""))[:40], "mode": "readwrite" if r.get("mode") == "readwrite" else "read"}
                 for r in info.get("roots", []) if r.get("name")][:20]
        with self._cv:
            self._info[device] = {"roots": roots, "platform": str(info.get("platform", ""))[:20], "version": str(info.get("version", ""))[:20],
                                  "last_seen": self.clock()}
            self._queues.setdefault(device, deque())

    def next_request(self, device: str, wait: float = 25.0) -> dict[str, Any] | None:
        """端末が次の操作を受け取る（無ければ wait 秒まで待つ）。呼ぶたびに「生きている」と記録する。"""
        deadline = self.clock() + wait
        with self._cv:
            self._info.setdefault(device, {"roots": [], "platform": "", "version": "", "last_seen": 0.0})["last_seen"] = self.clock()
            q = self._queues.setdefault(device, deque())
            while not q:
                left = deadline - self.clock()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 1.0))
                self._info[device]["last_seen"] = self.clock()
            req = q.popleft()
            return {"id": req["id"], "op": req["op"], "args": req["args"]}

    def submit_result(self, device: str, req_id: str, ok: bool, payload: Any) -> bool:
        """端末が結果を返す。別の端末宛ての要求への結果は受け付けない。"""
        with self._cv:
            p = self._pending.get(req_id)
            if p is None or p["device"] != device or p["event"].is_set():
                return False
            p["result"] = {"ok": bool(ok), ("data" if ok else "error"): payload}
            p["event"].set()
            return True

    # --- サーバー側（ツール経由）---
    def online(self, device: str) -> bool:
        with self._cv:
            i = self._info.get(device)
            return bool(i) and self.clock() - i["last_seen"] < self.offline_after

    def devices(self) -> dict[str, dict[str, Any]]:
        with self._cv:
            return {d: {**i, "online": self.clock() - i["last_seen"] < self.offline_after} for d, i in self._info.items()}

    def call(self, device: str, op: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """端末に操作を依頼し、結果を待つ。戻り値は {"ok": True, "data": ...} か {"ok": False, "error": ...}。"""
        if op not in OPS:
            return {"ok": False, "error": f"未対応の操作です: {op}"}
        if not self.online(device):
            return {"ok": False, "error": "device_offline", "reason": f"端末 {device} はオフラインです（アプリを起動して接続してください）"}
        req = {"id": uuid.uuid4().hex[:16], "op": op, "args": args}
        ev = threading.Event()
        with self._cv:
            q = self._queues.setdefault(device, deque())
            if len(q) >= MAX_QUEUE:
                return {"ok": False, "error": "queue_full"}
            self._pending[req["id"]] = {"device": device, "event": ev, "result": None}
            q.append(req)
            self._cv.notify_all()
        try:
            if not ev.wait(timeout):
                return {"ok": False, "error": "timeout", "reason": f"端末 {device} から {int(timeout)} 秒以内に応答がありませんでした"}
            with self._cv:
                res = self._pending[req["id"]]["result"]
            if res and len(str(res)) > MAX_RESULT_BYTES:
                return {"ok": False, "error": "too_large"}
            return res or {"ok": False, "error": "no_result"}
        finally:
            with self._cv:
                self._pending.pop(req["id"], None)
                try:
                    q.remove(req)  # 未配信のまま時間切れになった要求は取り下げる
                except ValueError:
                    pass
