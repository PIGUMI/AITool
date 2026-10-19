"""設定ファイル（TOML）の読み込み。"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "config.toml"


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """設定を読む。"""
    with open(path or DEFAULT_PATH, "rb") as f:
        return tomllib.load(f)
