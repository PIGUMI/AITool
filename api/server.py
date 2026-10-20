"""コアのHTTP API（REST＋SSE）。APIトークン必須。画面は持たない（ui/ の静的ファイルを配るだけ）。

管理用: /api/runs（作業一覧・詳細・中止）、/api/audit（監査ログ検索）、/api/tools、/api/dashboard、/api/devices。
"""
from __future__ import annotations

import hashlib
import json
import queue
import re
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.agent import Agent
from core.conversations import ConversationStore
from core.jobs import JobQueue
from core.slots import SlotBusy
from core.updater import UpdateError

from .tokens import TokenStore

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
HTML = "text/html; charset=utf-8"
# 認証なしで配る静的ファイル（画面本体・PWA 用）。トークンは画面側（ブラウザ）が持ち、API 呼び出しにだけ付ける。
STATIC = {
    "/": ("index.html", HTML), "/index.html": ("index.html", HTML), "/admin": ("admin.html", HTML), "/admin.html": ("admin.html", HTML),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
}
MAX_BODY = 64 * 1024


REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_EXE = REPO_ROOT / "dist" / "Faraway.exe"          # 準備（検証）のときにビルドされる。開発中のチェックアウトでは build.ps1 の出力
WINDOWS_SRC = REPO_ROOT / "apps" / "windows" / "Faraway.cs"


def windows_client_info() -> dict[str, Any]:
    """配布する Faraway.exe の版（ソースの Version 定数）・SHA-256・サイズ。無ければ available=False。"""
    m = re.search(r'const string Version = "([0-9.]+)"', WINDOWS_SRC.read_text(encoding="utf-8")) if WINDOWS_SRC.is_file() else None
    if not (WINDOWS_EXE.is_file() and m):
        return {"available": False}
    data = WINDOWS_EXE.read_bytes()
    return {"available": True, "version": m.group(1), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


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


def make_server(agent: Agent, tokens: TokenStore, host: str, port: int, jobs: JobQueue | None = None) -> ThreadingHTTPServer:
    """トークンが1つも無いサーバーは作らない（認証は必須）。"""
    if not tokens.devices():
        raise RuntimeError("APIトークンが未発行です。python -m api.tokens add <端末名> で発行してください")
    convs = ConversationStore()   # 会話の短期記憶（メモリのみ）

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
            if path in STATIC:
                name, ctype = STATIC[path]
                body = (UI_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            self._device = self._auth()
            if not self._device:
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
                self._json(200, {"devices": tokens.devices(), "bridge": agent.bridge.devices()})
            elif path in ("/api/client/windows", "/api/client/windows/download"):
                # アプリ（Faraway.exe）の自己更新用。サーバーが持つ最新の Faraway.exe の情報と本体（認証必須）。
                info = windows_client_info()
                if path.endswith("download"):
                    if not info["available"]:
                        return self._json(404, {"error": "Faraway.exe が用意されていません"})
                    data = WINDOWS_EXE.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json(200, info)
            elif path == "/api/update/status":
                self._json(200, agent.updater.status() if agent.updater else {"enabled": False})
            elif path == "/api/bridge/next":
                # 端末（アプリ）のロングポーリング。自分宛ての操作だけを受け取る。
                try:
                    wait = max(0.0, min(float(qs.get("wait", "25")), 30.0))
                except ValueError:
                    wait = 25.0
                self._json(200, {"request": agent.bridge.next_request(self._device, wait)})
            elif path == "/api/jobs":
                self._json(200, {"jobs": jobs.list() if jobs else [], "window": [str(jobs.window[0])[:5], str(jobs.window[1])[:5]] if jobs else None})
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
            if path.startswith("/api/update/") and path.rsplit("/", 1)[-1] in ("check", "prepare", "apply", "rollback"):
                device = self._auth()
                if not device:
                    return
                up = agent.updater
                if up is None:
                    return self._json(404, {"error": "自己更新が有効ではありません（[update] remote_url が未設定）"})
                action = path.rsplit("/", 1)[-1]
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n)) if 0 < n <= MAX_BODY else {}
                    if action == "check":
                        return self._json(200, up.check())
                    if action == "prepare":   # 数分かかるので、バックグラウンドで始める
                        agent.audit.write("update_prepare_requested", "-", device=device)
                        return self._json(202, up.prepare_async(body.get("sha") or None))
                    # apply / rollback は R2。画面のボタンも承認要求を作るだけで、実行は承認画面で承認されたときだけ
                    if action == "apply":
                        sha = str(body.get("sha", ""))
                        ap = agent.approvals.request("-", "update_apply", {"sha": sha}, up.apply_summary(sha))
                    else:
                        ap = agent.approvals.request("-", "update_rollback", {}, "Faraway を直前の版に戻して再起動します")
                    return self._json(200, {"approval_id": ap["id"], "hash": ap["hash"], "summary": ap["summary"]})
                except UpdateError as e:
                    return self._json(409, {"error": str(e)})
                except (ValueError, TypeError, AttributeError):
                    return self._json(400, {"error": "不正な本文です"})
            if path in ("/api/bridge/hello", "/api/bridge/result"):
                device = self._auth()
                if not device:
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n > 700_000:
                        return self._json(413, {"error": "too large"})
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if path.endswith("hello"):
                        agent.bridge.hello(device, body)
                        agent.audit.write("device_hello", "-", device=device, roots=[r.get("name") for r in body.get("roots", [])],
                                          platform=str(body.get("platform", ""))[:20])
                        return self._json(200, {"ok": True})
                    ok = agent.bridge.submit_result(device, str(body["id"]), bool(body.get("ok")), body.get("data") if body.get("ok") else body.get("error"))
                    return self._json(200 if ok else 404, {"ok": ok})
                except (ValueError, KeyError, TypeError, AttributeError):
                    return self._json(400, {"error": "不正な本文です"})
            if path == "/api/jobs" or (path.startswith("/api/jobs/") and path.endswith("/cancel")):
                device = self._auth()
                if not device:
                    return
                if jobs is None:
                    return self._json(404, {"error": "ジョブキューが有効ではありません"})
                if path.endswith("/cancel"):
                    out = jobs.cancel(path.split("/")[3])
                    return self._json(200 if out.get("ok") else (404 if out.get("error") == "not_found" else 409), out)
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n)) if 0 < n <= MAX_BODY else {}
                    request, when = str(body["request"]).strip(), str(body.get("when", "now"))
                    if not request:
                        raise ValueError
                    return self._json(200, jobs.submit(request, when, device))
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "request（と when: now/night）が必要です"})
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
                body = json.loads(self.rfile.read(n))
                request = str(body["request"]).strip()
                if not request:
                    raise ValueError
                cid = str(body.get("conversation", ""))   # 会話ID（画面が会話ごとに発行）。省略時は文脈なしの単発
            except (ValueError, KeyError, TypeError, AttributeError):
                return self._json(400, {"error": "request が必要です"})
            q: queue.Queue[dict[str, Any] | None] = queue.Queue()
            history, was_tainted = convs.get(device, cid)

            def work() -> None:
                try:
                    r = agent.run(request, on_event=q.put, device=device, history=history, tainted=was_tainted)
                    if r.stopped_reason == "done":   # 正常に終わったやり取りだけを、会話の文脈に残す
                        convs.add(device, cid, request, r.answer, r.tainted)
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


def enable_tls(server: ThreadingHTTPServer, cert_file: str, key_file: str) -> None:
    """待ち受けソケットを TLS にする（Tailscale の証明書など。HTTPS のときだけ PWA としてインストールできる）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)


def main() -> int:
    from core import lifecycle
    from core.app import build_agent
    from core.config import data_path, load_config
    from core.secrets import secrets_dir

    cfg = load_config()
    agent = build_agent()
    store = TokenStore(secrets_dir() / cfg["api"]["tokens_file"])
    hosts = cfg["api"].get("hosts") or [cfg["api"]["host"]]
    api = cfg["api"]
    tls_on = bool(api.get("tls_cert") and api.get("tls_key"))
    tls_hosts = set(api.get("tls_hosts") or hosts) if tls_on else set()   # TLS にするアドレス（省略すると全て）
    if not store.devices():  # 初回起動: トークンを自動発行し、開くだけで使えるURLを表示する
        token = store.add("auto-first")
        print("初回のためAPIトークンを自動発行しました。スマホのブラウザで次のURLを開くと設定されます（このURLは他人に見せないこと）:")
        for h in hosts:
            if h != "127.0.0.1":
                print(f"  {'https' if h in tls_hosts else 'http'}://{h}:{api['port']}/#token={token}")
    jobs = None
    batch = cfg.get("batch", {})
    if batch.get("enabled", True):  # 夜間バッチ（時間帯は仮。config の [batch]）
        jobs = JobQueue(agent, (batch.get("window_start", "02:00"), batch.get("window_end", "06:00")),
                        data_path(cfg["audit"]["directory"]).parent / "jobs.json")
        jobs.start()
    servers = []
    for h in hosts:  # LAN と Tailscale など、待ち受けるアドレスごとに1つ起動する
        try:
            srv = make_server(agent, store, h, cfg["api"]["port"], jobs)
            if h in tls_hosts:
                enable_tls(srv, api["tls_cert"], api["tls_key"])
            servers.append(srv)
            print(f"listening on {h}:{cfg['api']['port']}" + (" (HTTPS)" if h in tls_hosts else ""))
        except (OSError, ssl.SSLError) as e:  # 例: Tailscale が停止中でアドレスが無い。他のアドレスは続ける
            print(f"skip {h}: {e.strerror or type(e).__name__}")
    if not servers:
        raise SystemExit("待ち受けできるアドレスがありません")
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    exit_code = {"v": 0}

    def on_restart() -> None:
        """自己更新の適用後: サーバーを止め、終了コード 75 で終了する（スーパーバイザーが新しい版で起動し直す）。"""
        exit_code["v"] = lifecycle.RESTART_EXIT_CODE
        agent.audit.write("restarting", "-", reason="update")
        if jobs is not None:
            jobs.stop()
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    lifecycle.set_restart_handler(on_restart)
    up = agent.updater
    hours = float(cfg.get("update", {}).get("auto_check_hours", 6))
    if up is not None and hours > 0:   # 定期的に更新を確認する（確認だけ。適用は承認が必要）
        def auto_check() -> None:
            last = ""
            time.sleep(60)
            while True:
                try:
                    info = up.check()
                    if info["state"] == "behind" and info["latest"] != last:
                        last = info["latest"]
                        agent.audit.write("update_available", "-", latest=info["latest"][:12], commits=len(info["commits"]))
                except Exception:
                    pass
                time.sleep(hours * 3600)

        threading.Thread(target=auto_check, daemon=True, name="update-auto-check").start()
    servers[0].serve_forever()
    return exit_code["v"]


if __name__ == "__main__":
    sys.exit(main())
