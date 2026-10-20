"""ジョブキュー（夜間バッチ）。依頼を預かり、バッチ用スロットで1件ずつ実行する。

- when="now": 対話スロットが空いているときに実行（対話中は延期）
- when="night": 加えて、設定した時間帯（既定 02:00〜06:00、仮）の中だけ実行
- 実行は通常の依頼と同じエージェントループ（権限・承認・監査は同じ）。R2 は承認待ちで止まる
- キュー（未実行のもの）は JSON に保存し、再起動後も残る。実行中・完了の履歴は監査ログが正
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .agent import Agent

JST = timezone(timedelta(hours=9))
MAX_KEPT = 200
WHEN = ("now", "night")


def in_window(now: datetime, start: dtime, end: dtime) -> bool:
    """now.time() が [start, end) にあるか。日をまたぐ時間帯（22:00〜06:00 など）にも対応。"""
    t = now.timetz().replace(tzinfo=None)
    return start <= t < end if start <= end else (t >= start or t < end)


class JobQueue:
    def __init__(self, agent: Agent, window: tuple[str, str] = ("02:00", "06:00"), path: Path | str | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(JST), poll_seconds: float = 5.0) -> None:
        self.agent, self.clock, self.poll = agent, clock, poll_seconds
        self.window = (dtime.fromisoformat(window[0]), dtime.fromisoformat(window[1]))
        self.path = Path(path) if path else None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- 保存 ---
    def _load(self) -> None:
        if self.path and self.path.is_file():
            try:
                for j in json.loads(self.path.read_text(encoding="utf-8")):
                    self._jobs[j["id"]] = j
            except (OSError, ValueError, KeyError):
                pass  # 壊れた保存ファイルは無視（監査ログが正）

    def _save_locked(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            queued = [j for j in self._jobs.values() if j["status"] == "queued"]
            self.path.write_text(json.dumps(queued, ensure_ascii=False), encoding="utf-8")

    # --- 操作 ---
    def submit(self, request: str, when: str = "now", device: str = "-") -> dict[str, Any]:
        if when not in WHEN:
            raise ValueError("when は now か night")
        job = {"id": uuid.uuid4().hex[:10], "request": request[:2000], "when": when, "status": "queued", "device": device,
               "created_at": self.clock().timestamp(), "started_at": None, "finished_at": None, "run_id": None, "result": None}
        with self._lock:
            self._jobs[job["id"]] = job
            while len(self._jobs) > MAX_KEPT:
                self._jobs.pop(next(iter(self._jobs)))
            self._save_locked()
        self.agent.audit.write("job_queued", job["id"], request=request[:200], when=when, device=device)
        return dict(job)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in reversed(self._jobs.values())]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                return {"ok": False, "error": "not_found"}
            if j["status"] == "queued":
                j["status"], j["finished_at"] = "cancelled", self.clock().timestamp()
                self._save_locked()
                self.agent.audit.write("job_cancelled", job_id, was="queued")
                return {"ok": True, "status": "cancelled"}
            if j["status"] == "running" and j["run_id"]:
                ok = self.agent.tracker.cancel(j["run_id"])
                self.agent.audit.write("job_cancelled", job_id, was="running", accepted=ok)
                return {"ok": ok, "status": "cancel_requested" if ok else "running"}
            return {"ok": False, "error": f"not_cancellable:{j['status']}"}

    # --- 実行 ---
    def _eligible(self, j: dict[str, Any]) -> bool:
        if self.agent.slots.busy("interactive"):  # 対話中は延期
            return False
        return j["when"] == "now" or in_window(self.clock(), *self.window)

    def tick(self) -> bool:
        """実行できるジョブを1件、同期で実行する。実行したら True。"""
        with self._lock:
            if self.agent.slots.busy("batch") or any(j["status"] == "running" for j in self._jobs.values()):
                return False
            job = next((j for j in self._jobs.values() if j["status"] == "queued" and self._eligible(j)), None)
            if job is None:
                return False
            job["status"], job["started_at"], job["run_id"] = "running", self.clock().timestamp(), uuid.uuid4().hex[:12]
            self._save_locked()
            rid, request = job["run_id"], job["request"]
        self.agent.audit.write("job_started", job["id"], run_id=rid)
        try:
            r = self.agent.run(request, slot="batch", device=f"job:{job['id']}", request_id=rid)
            status = "done" if r.stopped_reason == "done" else r.stopped_reason
            result = r.answer[:2000]
        except Exception as e:  # 1件の失敗でキュー全体を止めない
            status, result = "failed", f"{type(e).__name__}"
        with self._lock:
            job["status"], job["result"], job["finished_at"] = status, result, self.clock().timestamp()
        self.agent.audit.write("job_finished", job["id"], status=status, run_id=rid)
        return True

    def start(self) -> None:
        if self._thread:
            return

        def loop() -> None:
            while not self._stop.wait(self.poll):
                try:
                    while self.tick():
                        pass
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, daemon=True, name="job-queue")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
