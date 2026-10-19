"""診断：llama-server の生のやり取りを記録する。APIキーは記録しない（ヘッダーは保存しない）。

使い方: python -m core.diagnose "GPUの状態は？"   → logs/diagnose-<時刻>.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import Agent
from .audit import AuditLog
from .config import load_config
from .llm import LLMClient, _urllib_transport
from .app import build_registry, llm_extra
from .secrets import read_secret


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?", default="GPUの状態は？")
    a = ap.parse_args()
    cfg = load_config()
    key = read_secret(cfg["llm"]["api_key_file"])
    exchanges: list[dict[str, Any]] = []

    def recording(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        data = _urllib_transport(url, headers, payload, timeout)  # headers は保存しない
        exchanges.append({"url": url.replace(key, "***"), "request": {k: v for k, v in payload.items() if k != "tools"},
                          "tool_names": [t["function"]["name"] for t in payload.get("tools", [])], "response": data})
        return data

    audit = AuditLog(cfg["audit"]["directory"], secrets=[key])
    llm = LLMClient(cfg["llm"]["endpoints"], key, cfg["llm"].get("model", "qwen3.5-9b"), cfg["llm"].get("timeout_seconds", 120),
                    transport=recording, extra=llm_extra(cfg))
    r = Agent(llm, build_registry(cfg), audit, cfg["agent"]["max_tool_calls"], cfg["agent"]["max_llm_turns"]).run(a.request)
    out = Path("logs") / f"diagnose-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    text = json.dumps({"answer": r.answer, "stopped_reason": r.stopped_reason, "exchanges": exchanges}, ensure_ascii=False, indent=2)
    out.write_text(text.replace(key, "***"), encoding="utf-8")
    print(f"記録しました: {out}（APIキーは含まれません）\n回答: {r.answer!r} / {r.stopped_reason}")
    for i, ex in enumerate(exchanges):
        m = ex["response"]["choices"][0]
        msg = m["message"]
        print(f"[{i}] finish_reason={m.get('finish_reason')} keys={sorted(msg)} content_len={len(msg.get('content') or '')} "
              f"reasoning_len={len(msg.get('reasoning_content') or '')} tool_calls={len(msg.get('tool_calls') or [])}")


if __name__ == "__main__":
    main()
