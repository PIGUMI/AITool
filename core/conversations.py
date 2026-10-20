"""会話の履歴（短期記憶）。同じ会話の中で、直前のやり取りを次の依頼の文脈として渡す。

- 端末×会話ID ごとに、直近の数往復の「依頼」と「最終回答」だけを持つ（ツールの戻り値は持たない）
- メモリ上だけ（再起動で消える）。一定時間使われない会話は忘れる
- 会話の中で、一度でも私的データ（端末のファイル）を読んだら、その会話では外部通信ツールを止め続ける
  （読んだ内容は回答に含まれ、履歴として次の依頼に残るため。新しい会話にすれば解除される）
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TURNS = 8
MAX_ANSWER_CHARS = 1500
MAX_TOTAL_CHARS = 6000
TTL_SECONDS = 6 * 3600
MAX_CONVERSATIONS = 200


class ConversationStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _expire_locked(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._items.items() if now - v["touched"] > TTL_SECONDS]:
            del self._items[k]
        while len(self._items) > MAX_CONVERSATIONS:
            del self._items[min(self._items, key=lambda k: self._items[k]["touched"])]

    def get(self, device: str, cid: str) -> tuple[list[dict[str, str]], bool]:
        """(履歴, 私的データを読んだか)。IDが不正・未知なら ([], False)。"""
        if not ID.match(cid or ""):
            return [], False
        with self._lock:
            self._expire_locked()
            c = self._items.get((device, cid))
            if c is None:
                return [], False
            msgs: list[dict[str, str]] = []
            for turn in c["turns"]:
                msgs += [{"role": "user", "content": turn[0]}, {"role": "assistant", "content": turn[1]}]
            return msgs, c["tainted"]

    def add(self, device: str, cid: str, request: str, answer: str, tainted: bool) -> None:
        """やり取りを追記する。空の回答は記録しない。履歴は直近 MAX_TURNS 往復・合計 MAX_TOTAL_CHARS 文字まで。"""
        if not ID.match(cid or "") or not answer.strip():
            return
        with self._lock:
            c = self._items.setdefault((device, cid), {"turns": [], "tainted": False, "touched": self.clock()})
            c["turns"].append((request[:1500], answer[:MAX_ANSWER_CHARS]))
            c["turns"] = c["turns"][-MAX_TURNS:]
            while len(c["turns"]) > 1 and sum(len(a) + len(b) for a, b in c["turns"]) > MAX_TOTAL_CHARS:
                c["turns"].pop(0)
            c["tainted"] = c["tainted"] or tainted
            c["touched"] = self.clock()
            self._expire_locked()
