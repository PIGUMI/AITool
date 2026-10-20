"""host_status（R0）：常駐PCの状態を読み取り専用で返す。値は加工せず、取れない項目は「取得不可」と返す。"""
from __future__ import annotations

import json
import platform
import re
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


_PS_COUNTERS = (
    "$ErrorActionPreference='Stop';"
    "$p=@(Get-Counter '\\Processor(_Total)\\% Processor Time','\\GPU Adapter Memory(*)\\Dedicated Usage','\\GPU Engine(*)\\Utilization Percentage').CounterSamples"
    " | ForEach-Object { @{n=$_.Path; i=$_.InstanceName; v=$_.CookedValue} };"
    "$p | ConvertTo-Json -Compress"
)
_ENGINE = re.compile(r"luid_(?P<luid>\w+_\w+)_phys_\d+_eng_\d+_engtype_(?P<type>.+)$", re.I)


def parse_counters(samples: list[dict[str, Any]], vram_total_mb: int | None = None) -> dict[str, Any]:
    """Windows の性能カウンターの値から、CPU 使用率と GPU の VRAM 使用量・使用率を求める。

    温度は性能カウンターに無いため返さない（取得不可）。使用率は、最も忙しいエンジン種別の合計（上限100）。
    """
    cpu = next((s["v"] for s in samples if "processor time" in s["n"].lower()), None)
    vram = [s["v"] for s in samples if "dedicated usage" in s["n"].lower()]
    groups: dict[tuple[str, str], float] = {}
    for s in samples:
        m = _ENGINE.search(s["i"]) if "gpu engine" in s["n"].lower() else None
        if m:
            key = (m["luid"], m["type"])
            groups[key] = groups.get(key, 0.0) + float(s["v"])
    gpu: dict[str, Any] = {"temperature_c": None, "temperature_note": "Windows の標準カウンターでは取得できません"}
    if vram:
        gpu["vram_used_mb"] = round(max(vram) / 1024 / 1024)
    if vram_total_mb:
        gpu["vram_total_mb"] = vram_total_mb
    if groups:
        gpu["utilization_percent"] = round(min(100.0, max(groups.values())), 1)
        gpu["utilization_note"] = "最も忙しいエンジン種別の使用率（近似値）"
    return {"cpu_percent": None if cpu is None else round(cpu, 1), "gpu": gpu}


def _windows_counters(cfg: dict[str, Any]) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise Unavailable("Windows 以外では性能カウンターを使えません")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_COUNTERS],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        raise Unavailable("性能カウンターを取得できませんでした")
    data = json.loads(r.stdout)
    return parse_counters(data if isinstance(data, list) else [data], cfg.get("gpu_vram_total_mb"))


def _gpu(cfg: dict[str, Any]) -> dict[str, Any]:
    cmd = cfg.get("gpu_command") or []
    if cmd:  # 設定された専用コマンドがあればそれを優先する
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout
        return json.loads(out)
    return _windows_counters(cfg)["gpu"]


def _memory_windows() -> dict[str, Any]:
    """GlobalMemoryStatusEx でメモリの合計と空きを取る（標準ライブラリのみ）。"""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
        raise Unavailable("メモリ情報を取得できませんでした")
    return {"memory_total_bytes": st.ullTotalPhys, "memory_available_bytes": st.ullAvailPhys, "memory_percent": st.dwMemoryLoad}


def _cpu_memory(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil  # 入っていれば使う（任意）
        vm = psutil.virtual_memory()
        return {"cpu_percent": psutil.cpu_percent(interval=0.5), "memory_total_bytes": vm.total,
                "memory_available_bytes": vm.available, "memory_percent": vm.percent}
    except ImportError:
        pass
    if platform.system() != "Windows":
        raise Unavailable("psutil が未導入で、Windows 以外の代替手段はありません")
    out = _memory_windows()
    out["cpu_percent"] = _windows_counters(cfg)["cpu_percent"]
    return out


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
