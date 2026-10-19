"""監査ログ（JSON Lines・日付別）。秘匿値はマスクし、削除はしない（圧縮のみ）。"""
from __future__ import annotations

import gzip
import json
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))
MASK = "***"
_SENSITIVE_KEY = re.compile(
    r"token|password|passwd|secret|api[_-]?key|authorization|credential|cookie", re.I
)
_BEARER = re.compile(r"(Bearer\s+)\S+", re.I)
_KV = re.compile(r"((?:token|password|passwd|secret|api[_-]?key)\s*[=:]\s*)\S+", re.I)


class AuditLog:
    """1行1イベントのJSON Lines監査ログ。"""

    def __init__(self, directory: Path | str, secrets: list[str] | None = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._secrets = [s for s in (secrets or []) if s]
        self._lock = threading.Lock()

    def add_secret(self, value: str) -> None:
        """値そのものをマスク対象に加える。"""
        if value:
            self._secrets.append(value)

    def mask(self, obj: Any) -> Any:
        """再帰的に秘匿値を伏せる。"""
        if isinstance(obj, dict):
            return {
                k: (MASK if _SENSITIVE_KEY.search(str(k)) else self.mask(v)) for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self.mask(v) for v in obj]
        if isinstance(obj, str):
            s = _BEARER.sub(r"\1" + MASK, obj)
            s = _KV.sub(r"\1" + MASK, s)
            for secret in self._secrets:
                s = s.replace(secret, MASK)
            return s
        return obj

    def path_for(self, when: datetime) -> Path:
        return self.dir / f"audit-{when.astimezone(JST):%Y-%m-%d}.jsonl"

    def write(self, event: str, request_id: str, **fields: Any) -> dict[str, Any]:
        """イベントを1行追記する。記録した（マスク後の）内容を返す。"""
        now = datetime.now(JST)
        record = self.mask({"ts": now.isoformat(timespec="milliseconds"), "request_id": request_id, "event": event, **fields})
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, open(self.path_for(now), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record

    def read(self, day: datetime | None = None) -> list[dict[str, Any]]:
        """指定日（既定は今日）のイベントを読む。"""
        p = self.path_for(day or datetime.now(JST))
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]

    def search(self, days: int = 3, text: str = "", event: str = "", tool: str = "", request_id: str = "",
               limit: int = 200) -> list[dict[str, Any]]:
        """直近 days 日（圧縮済みを含む）のイベントを新しい順に絞り込む。text は行全体への部分一致。"""
        now = datetime.now(JST)
        needle = text.lower()
        out: list[dict[str, Any]] = []
        for d in range(max(1, days)):
            base = self.path_for(now - timedelta(days=d))
            gz = base.with_name(base.name + ".gz")
            if base.exists():
                lines = base.read_text(encoding="utf-8").splitlines()
            elif gz.exists():
                with gzip.open(gz, "rt", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            else:
                continue
            for line in reversed(lines):
                if not line or (needle and needle not in line.lower()):
                    continue
                rec = json.loads(line)
                if (event and rec.get("event") != event) or (tool and rec.get("tool") != tool) \
                        or (request_id and rec.get("request_id") != request_id):
                    continue
                out.append(rec)
                if len(out) >= limit:
                    return out
        return out

    def compress_old(self) -> list[Path]:
        """今日より前のログを .gz に圧縮する（元の内容は残し、削除は圧縮済みの原本のみ）。"""
        today = self.path_for(datetime.now(JST))
        out = []
        for p in sorted(self.dir.glob("audit-*.jsonl")):
            if p == today:
                continue
            gz = p.with_name(p.name + ".gz")
            with open(p, "rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
            out.append(gz)
        return out
