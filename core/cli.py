"""CLI：python -m core.cli "GPUの状態は？" """
from __future__ import annotations

import argparse

from .app import build_agent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    ap.add_argument("--config")
    a = ap.parse_args()
    r = build_agent(a.config).run(a.request, on_event=lambda e: print(f"  [{e['tool']}: {e['status']}]") if e["type"] == "tool" else None)
    print(r.answer)


if __name__ == "__main__":
    main()
