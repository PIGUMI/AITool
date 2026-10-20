"""network（R0）：設定ファイルに列挙した機器だけの ping・ポート・Web 死活確認。LAN全体の走査はしない。"""
from __future__ import annotations

import platform
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable

from policy.levels import Level
from policy.registry import ToolSpec

CHECKS = ["ping", "ports", "web"]


def _ping(address: str, timeout: float) -> bool:
    win = platform.system() == "Windows"
    cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), address] if win else ["ping", "-c", "1", "-W", str(max(1, int(timeout))), address]
    return subprocess.run(cmd, capture_output=True, timeout=timeout + 3).returncode == 0


def _port(address: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def _web(url: str, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return {"alive": True, "http_status": r.status}
    except urllib.error.HTTPError as e:  # 応答はある
        return {"alive": True, "http_status": e.code}
    except Exception as e:
        return {"alive": False, "error": type(e).__name__}


def build_tool(cfg: dict[str, Any], ping: Callable[[str, float], bool] = _ping,
               port: Callable[[str, int, float], bool] = _port,
               web: Callable[[str, float], dict[str, Any]] = _web) -> ToolSpec:
    """network ツールを組み立てる。対象は cfg の機器（有効かつアドレスありのもの）だけ。"""
    timeout = float(cfg.get("timeout_seconds", 2))
    devices = {d["name"]: d for d in cfg.get("devices", []) if d.get("enabled") and d.get("address")}
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "device": {"type": "string", "enum": sorted(devices), "description": "対象機器名。省略すると有効な全機器。"},
            "checks": {"type": "array", "items": {"type": "string", "enum": CHECKS}},
        },
        "additionalProperties": False,
    }

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        names = [args["device"]] if args.get("device") else sorted(devices)
        checks = args.get("checks") or CHECKS
        out: dict[str, Any] = {}
        for n in names:
            d = devices[n]
            r: dict[str, Any] = {"address": d["address"]}
            if "ping" in checks:
                r["ping"] = ping(d["address"], timeout)
            if "ports" in checks and d.get("ports"):
                r["ports"] = {str(p): port(d["address"], p, timeout) for p in d["ports"]}
            if "web" in checks and d.get("web"):
                r["web"] = web(d["web"], timeout)
            out[n] = r
        return out

    return ToolSpec("network", "設定に登録された既知のLAN機器のping応答・ポート疎通・Webインターフェース死活を確認する。",
                    schema, Level.R0, handler)
