"""Google の OAuth 認証（初回のみ、ぴぐみさんが実行）: python -m tools.calendar.auth

ブラウザで同意すると refresh_token を %USERPROFILE%\\.assistant\\secrets\\google_token.json に保存する。
スコープは calendar.readonly だけ。PKCE + ループバック（127.0.0.1）方式で、トークンは画面やログに出さない。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from core.secrets import secrets_dir

from . import SCOPE, CalendarError, _http


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge[S256])。"""
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client: dict[str, Any], redirect: str, state: str, challenge: str) -> str:
    q = {"client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
         "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return client.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth") + "?" + urllib.parse.urlencode(q)


def main(base: Path | None = None) -> None:
    base = base or secrets_dir()
    cfile = base / "google_client.json"
    if not cfile.is_file():
        raise SystemExit(f"{cfile} がありません。Google Cloud Console で「デスクトップアプリ」の OAuth クライアントを作成し、JSON をここに置いてください。")
    raw = json.loads(cfile.read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web") or raw
    verifier, challenge = pkce_pair()
    state = pysecrets.token_urlsafe(16)
    got: dict[str, str] = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_GET(self) -> None:
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            ok = q.get("state") == state and "code" in q
            if ok:
                got["code"] = q["code"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("認証が完了しました。このタブは閉じて構いません。".encode() if ok else "認証に失敗しました。".encode())
            done.set()

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = auth_url(client, redirect, state, challenge)
    print("ブラウザで Google の同意画面を開きます。開かない場合は次の URL を開いてください:\n" + url)
    webbrowser.open(url)
    if not done.wait(300) or "code" not in got:
        raise SystemExit("認証が完了しませんでした")
    srv.shutdown()
    body = urllib.parse.urlencode({"client_id": client["client_id"], "client_secret": client.get("client_secret", ""), "code": got["code"],
                                   "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    try:
        r = _http(client.get("token_uri", "https://oauth2.googleapis.com/token"), {"Content-Type": "application/x-www-form-urlencoded"}, body)
    except CalendarError as e:
        raise SystemExit(str(e))
    if "refresh_token" not in r:
        raise SystemExit("refresh_token が返りませんでした。Google アカウントの「アプリのアクセス権」から一度削除して、もう一度実行してください。")
    (base / "google_token.json").write_text(json.dumps({"refresh_token": r["refresh_token"]}), encoding="utf-8")
    print("保存しました（トークンの値は表示しません）。サーバーを再起動すると calendar ツールが有効になります。")


if __name__ == "__main__":
    main()
