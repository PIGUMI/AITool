"""コアのHTTP API（REST＋SSE）。APIトークン必須。画面は持たない（ui/ の静的ファイルを配るだけ）。"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.agent import Agent
from core.slots import SlotBusy

from .tokens import TokenStore

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
MAX_BODY = 64 * 1024


def make_server(agent: Agent, tokens: TokenStore, host: str, port: int) -> ThreadingHTTPServer:
    """トークンが1つも無いサーバーは作らない（認証は必須）。"""
    if not tokens.devices():
        raise RuntimeError("APIトークンが未発行です。python -m api.tokens add <端末名> で発行してください")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # 標準出力にトークン等を出さない
            pass

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> str | None:
            h = self.headers.get("Authorization", "")
            device = tokens.verify(h[7:] if h.lower().startswith("bearer ") else None)
            if device is None:
                agent.audit.write("auth_failed", "-", path=self.path, client=self.client_address[0])
                self._json(401, {"error": "unauthorized"})
            return device

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                body = (UI_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/health":
                if self._auth():
                    self._json(200, {"status": "ok", "tools": agent.registry.names()})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/chat":
                return self._json(404, {"error": "not found"})
            device = self._auth()
            if not device:
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > MAX_BODY:
                    return self._json(413, {"error": "too large"})
                request = str(json.loads(self.rfile.read(n))["request"]).strip()
                if not request:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "request が必要です"})
            q: queue.Queue[dict[str, Any] | None] = queue.Queue()

            def work() -> None:
                try:
                    r = agent.run(request, on_event=q.put)
                    q.put({"type": "done", "request_id": r.request_id, "stopped_reason": r.stopped_reason,
                           "tool_calls": r.tool_calls})
                except SlotBusy as e:
                    q.put({"type": "error", "text": str(e)})
                except Exception as e:
                    q.put({"type": "error", "text": f"内部エラー: {type(e).__name__}"})
                q.put(None)

            threading.Thread(target=work, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while (ev := q.get()) is not None:
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    from core.app import build_agent
    from core.config import load_config
    from core.secrets import secrets_dir

    cfg = load_config()
    srv = make_server(build_agent(), TokenStore(secrets_dir() / cfg["api"]["tokens_file"]),
                      cfg["api"]["host"], cfg["api"]["port"])
    print(f"listening on {cfg['api']['host']}:{cfg['api']['port']}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
