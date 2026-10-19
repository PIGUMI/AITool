"""llama.cpp のスロット管理。対話用とバッチ用を分け、対話がバッチに待たされないようにする。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class SlotBusy(Exception):
    """スロットが空かなかった。"""


class SlotPool:
    def __init__(self, interactive: int = 1, batch: int = 1) -> None:
        self._sems = {"interactive": threading.BoundedSemaphore(interactive),
                      "batch": threading.BoundedSemaphore(batch)}

    @contextmanager
    def acquire(self, kind: str = "interactive", timeout: float = 600) -> Iterator[None]:
        sem = self._sems.get(kind)
        if sem is None:
            raise ValueError(f"未知のスロット種別: {kind}")
        if not sem.acquire(timeout=timeout):
            raise SlotBusy(f"{kind} スロットが空きません")
        try:
            yield
        finally:
            sem.release()
