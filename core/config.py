"""設定ファイル（TOML）の読み込み。

外部の設定（環境変数 FARAWAY_CONFIG、または %USERPROFILE%\\.assistant\\config.toml）があればそれを使う。
自己更新でコード（リリース）が入れ替わっても、設定が消えたり上書きされたりしないようにするため。
無ければリポジトリ内の config/config.toml（初期値・サンプル）を使う。
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "config.toml"


def external_config_path() -> Path | None:
    """外部の設定ファイル。環境変数 FARAWAY_CONFIG が最優先、次に ~/.assistant/config.toml。"""
    env = os.environ.get("FARAWAY_CONFIG")
    if env:
        return Path(env)
    p = Path.home() / ".assistant" / "config.toml"
    return p if p.is_file() else None


def data_path(p: str | Path) -> Path:
    """データ（監査ログなど）の保存先。相対パスは、環境変数 FARAWAY_DATA_DIR（自己更新のインストール先の data/）を基準にする。

    リリースのフォルダ（更新のたびに入れ替わる）の中にデータを作らないため。未設定ならカレントディレクトリ基準（従来どおり）。
    """
    path = Path(p)
    base = os.environ.get("FARAWAY_DATA_DIR")
    return path if path.is_absolute() or not base else Path(base) / path


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """設定を読む。path 指定があればそれ、無ければ外部の設定 → リポジトリ内の既定の順。"""
    with open(path or external_config_path() or DEFAULT_PATH, "rb") as f:
        return tomllib.load(f)
