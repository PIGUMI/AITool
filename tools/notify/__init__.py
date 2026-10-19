"""notify（R1）：ntfy へ通知を送る。通知先は config で固定し、LAN/ローカル/Tailscale の中のサーバーだけ許可する。

通知本文に秘匿情報を含めない（キー・トークン・パスワードらしい文字列は送信前に拒否する）。
夜間ビルドの結果・承認要求・PCの異常などに使う。ユーザーが頼んだとき、または仕様で決めた通知のときだけ送る。
"""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from policy.levels import Level
from policy.registry import ToolSpec

SECRET_LIKE = re.compile(r"(bearer\s+\S+|(token|password|passwd|secret|api[_-]?key)\s*[=:]\s*\S+|[A-Za-z0-9_\-]{40,})", re.I)
Sender = Callable[[str, dict[str, str], bytes, float], int]


def _send(url: str, headers: dict[str, str], body: bytes, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def is_private_host(host: str) -> bool:
    """host が（名前解決の結果も含め）全て非公開アドレスか。公開インターネット宛ての通知を防ぐ。"""
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    return bool(ips) and all(not ipaddress.ip_address(ip).is_global for ip in ips)


def build_tool(cfg: dict[str, Any], sender: Sender = _send, private_check: Callable[[str], bool] = is_private_host) -> ToolSpec | None:
    """notify を組み立てる。url が未設定なら None（登録しない）。"""
    url = cfg.get("url", "").rstrip("/")
    topic = cfg.get("topic", "")
    if not url or not topic:
        return None
    timeout = float(cfg.get("timeout_seconds", 5))
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"title": {"type": "string", "maxLength": 80}, "message": {"type": "string", "maxLength": 500},
                       "priority": {"type": "string", "enum": ["low", "default", "high"]}},
        "required": ["message"],
        "additionalProperties": False,
    }

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        title, message = args.get("title", ""), args["message"]
        if SECRET_LIKE.search(title) or SECRET_LIKE.search(message):
            return {"sent": False, "error": "blocked", "reason": "秘匿情報らしい文字列を含むため送信しません"}
        host = urllib.parse.urlsplit(url).hostname or ""
        if not private_check(host):
            return {"sent": False, "error": "blocked", "reason": "通知先が LAN/ローカル/Tailscale のアドレスではありません"}
        headers = {"Priority": {"low": "2", "default": "3", "high": "4"}[args.get("priority", "default")]}
        if title:
            headers["Title"] = urllib.parse.quote(title)  # ヘッダは ASCII のみ。ntfy は URL エンコードを解釈する
        try:
            status = sender(f"{url}/{urllib.parse.quote(topic)}", headers, message.encode("utf-8"), timeout)
        except (urllib.error.URLError, OSError) as e:
            return {"sent": False, "error": "send_failed", "reason": type(e).__name__}
        return {"sent": 200 <= status < 300, "http_status": status}

    return ToolSpec("notify", "スマートフォン等へ ntfy で通知を送る（短い本文のみ。秘匿情報は送れない）。ユーザーが頼んだときだけ使う。", schema, Level.R1, handler, egress=True)
