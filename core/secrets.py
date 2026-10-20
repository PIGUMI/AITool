"""シークレットの読み込み（%USERPROFILE%\\.assistant\\secrets\\ から読むだけ）。"""
from __future__ import annotations

import os
from pathlib import Path


def secrets_dir() -> Path:
    """シークレットの置き場所。環境変数 ASSISTANT_SECRETS_DIR があればそれを優先する（テスト用）。"""
    override = os.environ.get("ASSISTANT_SECRETS_DIR")
    return Path(override) if override else Path.home() / ".assistant" / "secrets"


def read_secret(name: str, base: Path | None = None) -> str:
    """ファイルからシークレットを読む。無ければ FileNotFoundError（値は例外文に含めない）。"""
    p = (base or secrets_dir()) / name
    if not p.is_file():
        raise FileNotFoundError(f"シークレットが見つかりません: {p}")
    return p.read_text(encoding="utf-8").strip()
