"""コアのHTTP API（REST＋SSE）。APIトークン必須。画面は持たない（ui/ の静的ファイルを配るだけ）。

管理用: /api/runs（作業一覧・詳細・中止）、/api/audit（監査ログ検索）、/api/tools、/api/dashboard、/api/devices。
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.agent import Agent
from core.slots import SlotBusy

from .tokens import TokenStore

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
PAGES = {"/": "index.html", "/index.html": "index.html", "/admin": "admin.html", "/admin.html": "admin.html"}
MAX_BODY = 64 * 1024


def dashboard(agent: Agent) -> dict[str, Any]:
    """host_status と network（どちらも R0）を実行して並べる。取れない項目はツールの戻り値のまま返す。"""
    out: dict[str, Any] = {}
    for name in ("host_status", "network"):
        spec = agent.registry.get(name)
        if spec is None:
            out[name] = {"error": "ツールが登録されていません"}
            continue
        try:
            out[name] = spec.handler({})
        except Exception as e:  # 1つの失敗で画面全体を壊さない
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    out["runs_running"] = sum(1 for r in agent.tracker.list() if r["status"] == "running")
    out["approvals_pending"] = agent.approvals.pending_count()
    return out


def make_server(agent: Agent, tokens: TokenStore, host: str, port: int) -> ThreadingHTTPServer:
    """トークンが1つも無いサーバーは作らない（認証は必須）。"""
    if not tokens.devices():
        raise RuntimeError("APIトークンが未発行です。python -m api.tokens add <端末名> で発行してください")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # 標準出力にトークン等を出さない
            pass

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> str | None:
            h = self.headers.get("Authorization", "")
            device = tokens.verify(h[7:] if h.lower().startswith("bearer ") else None)
            if device is None:
                agent.audit.write("auth_failed", "-", path=urlsplit(self.path).path, client=self.client_address[0])
                self._json(401, {"error": "unauthorized"})
            return device

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path, qs = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
            if path in PAGES:
                body = (UI_DIR / PAGES[path]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._auth():
                return
            if path == "/api/health":
                self._json(200, {"status": "ok", "tools": agent.registry.names()})
            elif path == "/api/runs":
                self._json(200, {"runs": agent.tracker.list()})
            elif path.startswith("/api/runs/"):
                rid = path.split("/")[3]
                run = agent.tracker.get(rid)
                events = agent.audit.search(days=7, request_id=rid, limit=500)[::-1]
                if run is None and not events:
                    return self._json(404, {"error": "not found"})
                self._json(200, {"run": run, "audit": events})
            elif path == "/api/audit":
                try:
                    limit = min(int(qs.get("limit", "100")), 500)
                    days = min(int(qs.get("days", "3")), 90)
                except ValueError:
                    return self._json(400, {"error": "limit/days は整数で指定してください"})
                self._json(200, {"events": agent.audit.search(days, qs.get("q", ""), qs.get("event", ""), qs.get("tool", ""),
                                                              qs.get("request_id", ""), limit)})
            elif path == "/api/tools":
                self._json(200, {"tools": [{"name": n, "level": agent.registry.get(n).level.name,
                                            "description": agent.registry.get(n).description} for n in agent.registry.names()]})
            elif path == "/api/dashboard":
                self._json(200, dashboard(agent))
            elif path == "/api/devices":
                self._json(200, {"devices": tokens.devices()})
            elif path == "/api/approvals":
                self._json(200, {"approvals": agent.approvals.list(), "ttl_seconds": agent.approvals.ttl})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/runs/") and path.endswith("/cancel"):
                device = self._auth()
                if not device:
                    return
                rid = path.split("/")[3]
                ok = agent.tracker.cancel(rid)
                agent.audit.write("cancel_requested", rid, device=device, accepted=ok)
                return self._json(200 if ok else 404, {"cancel_requested": ok})
            if path.startswith("/api/approvals/") and path.rsplit("/", 1)[-1] in ("approve", "reject"):
                device = self._auth()
                if not device:
                    return
                aid, action = path.split("/")[3], path.rsplit("/", 1)[-1]
                if action == "reject":
                    out = agent.approvals.reject(aid, device)
                else:
                    try:
                        n = int(self.headers.get("Content-Length", "0"))
                        shown = str(json.loads(self.rfile.read(n) or b"{}").get("hash", "")) if 0 < n <= MAX_BODY else ""
                    except (ValueError, TypeError, AttributeError):
                        shown = ""
                    out = agent.approvals.approve(aid, shown, device)  # 画面で見せたハッシュと一致したときだけ実行される
                code = 200 if out.get("ok") else (404 if out.get("error") == "not_found" else 409)
                return self._json(code, out)
            if path != "/api/chat":
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
                    r = agent.run(request, on_event=q.put, device=device)
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
    agent = build_agent()
    store = TokenStore(secrets_dir() / cfg["api"]["tokens_file"])
    hosts = cfg["api"].get("hosts") or [cfg["api"]["host"]]
    if not store.devices():  # 初回起動: トークンを自動発行し、開くだけで使えるURLを表示する
        token = store.add("auto-first")
        print("初回のためAPIトークンを自動発行しました。スマホのブラウザで次のURLを開くと設定されます（このURLは他人に見せないこと）:")
        for h in hosts:
            if h != "127.0.0.1":
                print(f"  http://{h}:{cfg['api']['port']}/#token={token}")
    servers = []
    for h in hosts:  # LAN と Tailscale など、待ち受けるアドレスごとに1つ起動する
        try:
            servers.append(make_server(agent, store, h, cfg["api"]["port"]))
            print(f"listening on {h}:{cfg['api']['port']}")
        except OSError as e:  # 例: Tailscale が停止中でアドレスが無い。他のアドレスは続ける
            print(f"skip {h}: {e.strerror or type(e).__name__}")
    if not servers:
        raise SystemExit("待ち受けできるアドレスがありません")
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
