"""host_status（R0）：常駐PCの状態を読み取り専用で返す。値は加工せず、取れない項目は「取得不可」と返す。"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable

from policy.levels import Level
from policy.registry import ToolSpec

SECTIONS = ["gpu", "cpu_memory", "disk", "llama", "recent_errors"]

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "description": "取得する項目。省略すると全項目。",
            "items": {"type": "string", "enum": SECTIONS},
        }
    },
    "additionalProperties": False,
}


class Unavailable(Exception):
    """その項目は取得できない。"""


def _gpu(cfg: dict[str, Any]) -> dict[str, Any]:
    cmd = cfg.get("gpu_command") or []
    if not cmd:
        raise Unavailable("GPU取得コマンドが config の host_status.gpu_command に未設定です")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout
    return json.loads(out)


def _cpu_memory(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil  # 任意の依存。無ければ取得不可として扱う
    except ImportError as e:
        raise Unavailable("psutil が未導入です") from e
    vm = psutil.virtual_memory()
    return {"cpu_percent": psutil.cpu_percent(interval=0.5), "memory_total_bytes": vm.total,
            "memory_available_bytes": vm.available, "memory_percent": vm.percent}


def _disk(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for p in cfg.get("disk_paths", []):
        u = shutil.disk_usage(p)
        out[p] = {"total_bytes": u.total, "used_bytes": u.used, "free_bytes": u.free}
    return out


def _llama(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for base in cfg.get("_llm_endpoints", []):
        url = base.rstrip("/").removesuffix("/v1") + "/health"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                out[url] = {"alive": True, "http_status": r.status}
        except Exception as e:
            out[url] = {"alive": False, "error": type(e).__name__}
    return out


def _recent_errors(cfg: dict[str, Any]) -> dict[str, Any]:
    n = int(cfg.get("log_tail_lines", 200))
    out = {}
    for f in cfg.get("log_files", []):
        p = Path(f)
        if not p.is_file():
            out[f] = {"error": "ファイルがありません"}
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        out[f] = [x for x in lines if "error" in x.lower()][-20:]
    return out


DEFAULT_COLLECTORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "gpu": _gpu, "cpu_memory": _cpu_memory, "disk": _disk, "llama": _llama, "recent_errors": _recent_errors,
}


def build_tool(cfg: dict[str, Any], llm_endpoints: list[str] | None = None,
               collectors: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None) -> ToolSpec:
    """host_status ツールを組み立てる。collectors を差し替えればモックでテストできる。"""
    cols = collectors or DEFAULT_COLLECTORS
    full_cfg = {**cfg, "_llm_endpoints": llm_endpoints or []}

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in args.get("sections") or SECTIONS:
            try:
                result[name] = {"status": "ok", "data": cols[name](full_cfg)}
            except Unavailable as e:
                result[name] = {"status": "unavailable", "reason": str(e)}
            except Exception as e:
                result[name] = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        return result

    return ToolSpec("host_status", "常駐PCの状態（GPU温度・使用率・VRAM、CPU・メモリ、ディスク空き、llama.cppの生死、直近のエラー）を取得する。",
                    INPUT_SCHEMA, Level.R0, handler)
