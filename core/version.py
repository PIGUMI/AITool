"""実行中の Faraway のバージョン（コミット SHA）。

- リリースフォルダ（自己更新で作られる）: 直下の RELEASE ファイルに SHA が書いてある
- 開発中のチェックアウト: git rev-parse HEAD
- どちらも無ければ "unknown"
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def current_version(root: Path = ROOT) -> str:
    f = root / "RELEASE"
    if f.is_file():
        return f.read_text(encoding="utf-8").strip()
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"
