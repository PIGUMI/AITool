"""current_time（R0）：現在の日時。モデルは日付を知らないので、日付に関わる質問ではこのツールを使う。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from policy.levels import Level
from policy.registry import ToolSpec

WEEKDAYS = "月火水木金土日"


def build_tool(tz_hours: int = 9, now: Callable[[], datetime] | None = None) -> ToolSpec:
    """既定は Asia/Tokyo（UTC+9）。now はテストで差し替える。"""
    tz = timezone(timedelta(hours=tz_hours))
    schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        t = (now or (lambda: datetime.now(timezone.utc)))().astimezone(tz)
        return {"iso": t.isoformat(timespec="seconds"), "date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M"),
                "weekday": WEEKDAYS[t.weekday()] + "曜日", "timezone": "Asia/Tokyo" if tz_hours == 9 else f"UTC{tz_hours:+d}"}

    return ToolSpec("current_time", "現在の日付・時刻・曜日を返す（Asia/Tokyo）。", schema, Level.R0, handler)
