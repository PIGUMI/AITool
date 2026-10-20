"""APIトークン（端末ごと）。保存するのはハッシュだけ。失くした端末だけ失効できる。"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets as pysecrets
from pathlib import Path


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, device: str) -> str:
        """端末用トークンを発行する（平文はこの戻り値でしか見られない）。"""
        data = self._load()
        token = pysecrets.token_urlsafe(32)
        data[device] = _h(token)
        self._save(data)
        return token

    def revoke(self, device: str) -> bool:
        data = self._load()
        found = data.pop(device, None) is not None
        self._save(data)
        return found

    def devices(self) -> list[str]:
        return sorted(self._load())

    def verify(self, token: str | None) -> str | None:
        """有効なら端末名、無効なら None。"""
        if not token:
            return None
        digest = _h(token)
        match = None
        for dev, stored in self._load().items():
            if hmac.compare_digest(stored, digest):
                match = dev
        return match


if __name__ == "__main__":
    import argparse

    from core.config import load_config
    from core.secrets import secrets_dir

    ap = argparse.ArgumentParser(description="APIトークンの発行・失効")
    ap.add_argument("action", choices=["add", "revoke", "list"])
    ap.add_argument("device", nargs="?")
    a = ap.parse_args()
    store = TokenStore(secrets_dir() / load_config()["api"]["tokens_file"])
    if a.action == "add":
        t = store.add(a.device)
        print(f"トークン（この一度しか表示されません）: {t}")
        cfg = load_config()["api"]
        for h in cfg.get("hosts", []):
            if h != "127.0.0.1":
                print(f"  開くだけで設定されるURL: http://{h}:{cfg['port']}/#token={t}")
    elif a.action == "revoke":
        print("失効しました" if store.revoke(a.device) else "その端末はありません")
    else:
        print("\n".join(store.devices()))
