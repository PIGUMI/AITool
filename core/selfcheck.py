"""自己検査: 新しい版が、現在の設定で組み立てられるかを確認する（モデルへは接続しない）。

自己更新の「準備」で、新しいリリースのフォルダから実行する:  python -m core.selfcheck
設定の項目が変わって読み込めない、ツールの登録に失敗する、といった問題を、切り替える前に見つける。
終了コード 0 = 問題なし。
"""
from __future__ import annotations

import sys
import tempfile


def main() -> int:
    from core.audit import AuditLog
    from core.bridge import DeviceBridge
    from core.app import build_registry
    from core.config import load_config

    cfg = load_config()
    for key in ("llm", "agent", "slots", "audit", "api"):
        if key not in cfg:
            print(f"設定に [{key}] がありません")
            return 1
    with tempfile.TemporaryDirectory() as d:
        reg = build_registry(cfg, AuditLog(d), DeviceBridge())
        if not reg.names():
            print("ツールが1つも登録されませんでした")
            return 1
        for name in reg.names():  # すべてのツールが権限レベルを持つこと
            if reg.get(name).level is None:
                print(f"権限レベルが無いツール: {name}")
                return 1
    print(f"ok: tools={len(reg.names())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
