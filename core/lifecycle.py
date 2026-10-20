"""プロセスの再起動要求。自己更新の適用後に、スーパーバイザーが新しい版で起動し直せるよう、決められた終了コードで終了する。

スーパーバイザー（core/supervisor.py）経由で起動されたときだけ、再起動できる（環境変数 FARAWAY_SUPERVISED=1）。
直接起動されたサーバーは、終了すると誰も起動し直さないので、自己更新は拒否する。
"""
from __future__ import annotations

import os
import threading
from typing import Callable

RESTART_EXIT_CODE = 75   # スーパーバイザーが「更新の適用による再起動」と解釈する終了コード

_callback: Callable[[], None] | None = None


def supervised() -> bool:
    return os.environ.get("FARAWAY_SUPERVISED") == "1"


def set_restart_handler(cb: Callable[[], None]) -> None:
    """再起動要求を受けたときの処理（サーバーの停止と終了コードの設定）を登録する。"""
    global _callback
    _callback = cb


def request_restart(delay: float = 2.0) -> bool:
    """delay 秒後に再起動を要求する（承認の応答などを返し終えてから止まるため）。登録が無ければ False。"""
    if _callback is None or not supervised():
        return False
    threading.Timer(delay, _callback).start()
    return True
