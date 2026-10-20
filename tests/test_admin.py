"""管理機能（作業の追跡・中止、監査ログ検索、管理API）のテスト。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from api.server import make_server
from api.tokens import TokenStore
from core.agent import Agent
from core.runs import RunTracker
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call


def reg_with(handler=lambda a: {"gpu": "mock"}):
    reg = ToolRegistry()
    schema = {"type": "object", "properties": {}}
    reg.register(ToolSpec("host_status", "GPU等の状態", schema, Level.R0, handler))
    reg.register(ToolSpec("network", "LAN機器", schema, Level.R0, lambda a: {"dev": {"ping": True}}))
    return reg


def test_tracker_lifecycle_and_limits():
    t = RunTracker(max_runs=2)
    t.start("a", "q1", "interactive")
    t.event("a", {"type": "tool", "tool": "x", "status": "ok"})
    assert t.get("a")["tool_calls"] == 1 and t.get("a")["status"] == "running"
    t.finish("a", "done", "回答", 1)
    assert t.get("a")["status"] == "done" and t.get("a")["finished_at"]
    t.start("b", "q2", "batch")
    t.start("c", "q3", "batch")
    assert t.get("a") is None and [r["request_id"] for r in t.list()] == ["c", "b"]
    assert "events" not in t.list()[0]


def test_cancel_only_running():
    t = RunTracker()
    t.start("a", "q", "interactive")
    assert t.cancel("a") is True and t.is_cancelled("a")
    t.finish("a", "cancelled", "", 0)
    assert t.cancel("a") is False and t.cancel("nope") is False and not t.is_cancelled("a")


def test_agent_records_run_and_device(audit):
    agent = Agent(ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("host_status", {})]},
                               {"role": "assistant", "content": "正常です"}]), reg_with(), audit)
    r = agent.run("状態は？", device="phone")
    run = agent.tracker.get(r.request_id)
    assert run["status"] == "done" and run["tool_calls"] == 1 and run["answer"] == "正常です" and run["device"] == "phone"
    assert [e["type"] for e in run["events"]] == ["tool", "message"]
    assert any(e["event"] == "request" and e["device"] == "phone" for e in audit.read())


def test_agent_cancel_stops_before_next_model_call(audit):
    holder = {}

    def handler(args):
        holder["agent"].tracker.cancel(holder["agent"].tracker.list()[0]["request_id"])  # ツール実行中に中止要求
        return {"ok": True}

    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("host_status", {})]},
                       {"role": "assistant", "content": "到達してはいけない"}])
    agent = holder["agent"] = Agent(llm, reg_with(handler), audit)
    r = agent.run("長い作業")
    assert r.stopped_reason == "cancelled" and len(llm.seen) == 1
    assert agent.tracker.get(r.request_id)["status"] == "cancelled"
    assert any(e["event"] == "cancelled" for e in audit.read())


def test_agent_run_marks_error_when_llm_crashes(audit):
    class Boom:
        def chat(self, m, t=None):
            raise RuntimeError("x")

    agent = Agent(Boom(), reg_with(), audit)
    with pytest.raises(RuntimeError):
        agent.run("q")
    assert agent.tracker.list()[0]["status"] == "error"


def test_audit_search_filters(audit):
    audit.write("request", "r1", request="GPUの状態")
    audit.write("tool_call", "r1", tool="host_status", status="ok")
    audit.write("tool_call", "r2", tool="network", status="ok")
    audit.write("request", "r2", request="pingして")
    assert len(audit.search()) == 4
    assert [e["tool"] for e in audit.search(tool="network")] == ["network"]
    assert {e["request_id"] for e in audit.search(request_id="r1")} == {"r1"}
    assert [e["request"] for e in audit.search(event="request", text="ping")] == ["pingして"]
    assert audit.search()[0]["request_id"] == "r2"  # 新しい順
    assert len(audit.search(limit=2)) == 2


def test_audit_search_reads_compressed_logs(audit):
    audit.write("request", "r1", request="古い")
    # 当日分は圧縮されないので、前日付のファイルを作って圧縮する
    from datetime import datetime, timedelta
    from core.audit import JST
    yesterday = audit.path_for(datetime.now(JST) - timedelta(days=1))
    yesterday.write_text(json.dumps({"ts": "x", "request_id": "old", "event": "request", "request": "昨日"}) + "\n", encoding="utf-8")
    audit.compress_old()
    assert not yesterday.exists()
    assert [e["request_id"] for e in audit.search(days=2, request_id="old")] == ["old"]


@pytest.fixture
def srv(tmp_path, audit):
    agent = Agent(ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("host_status", {})]},
                               {"role": "assistant", "content": "GPUは正常です"}]), reg_with(), audit)
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    s = make_server(agent, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", token, agent
    s.shutdown()


def get(url, token=None, method=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return urllib.request.urlopen(urllib.request.Request(url, headers=h, method=method), timeout=10)


@pytest.mark.parametrize("path", ["/api/runs", "/api/runs/abc", "/api/audit", "/api/tools", "/api/dashboard", "/api/devices"])
def test_admin_endpoints_require_token(srv, path):
    base, *_ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + path)
    assert e.value.code == 401


def test_admin_page_served(srv):
    base, *_ = srv
    assert "管理" in get(base + "/admin").read().decode()


def test_runs_list_detail_and_audit_search(srv):
    base, token, agent = srv
    urllib.request.urlopen(urllib.request.Request(base + "/api/chat", data=json.dumps({"request": "GPUの状態は？"}).encode(),
                                                  headers={"Authorization": f"Bearer {token}"}), timeout=10).read()
    runs = json.load(get(base + "/api/runs", token))["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "done" and runs[0]["device"] == "phone"
    detail = json.load(get(base + f"/api/runs/{runs[0]['request_id']}", token))
    assert detail["run"]["answer"] == "GPUは正常です"
    assert {"request", "tool_call", "response"} <= {e["event"] for e in detail["audit"]}
    ev = json.load(get(base + "/api/audit?event=tool_call&tool=host_status", token))["events"]
    assert len(ev) == 1 and ev[0]["tool"] == "host_status"


def test_unknown_run_is_404_and_cancel_of_finished_run_is_rejected(srv):
    base, token, agent = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/runs/zzz", token)
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/runs/zzz/cancel", token, method="POST")
    assert e.value.code == 404


def test_cancel_via_api_is_audited(srv, audit):
    base, token, agent = srv
    agent.tracker.start("live1", "実行中の作業", "interactive")
    r = get(base + "/api/runs/live1/cancel", token, method="POST")
    assert json.load(r)["cancel_requested"] is True and agent.tracker.is_cancelled("live1")
    assert any(e["event"] == "cancel_requested" and e["device"] == "phone" for e in audit.read())


def test_tools_dashboard_devices(srv):
    base, token, _ = srv
    tools = {t["name"]: t for t in json.load(get(base + "/api/tools", token))["tools"]}
    assert tools["host_status"]["level"] == "R0"
    d = json.load(get(base + "/api/dashboard", token))
    assert d["host_status"] == {"gpu": "mock"} and d["network"]["dev"]["ping"] is True and d["runs_running"] == 0
    assert json.load(get(base + "/api/devices", token))["devices"] == ["phone"]


def test_bad_audit_params(srv):
    base, token, _ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/audit?limit=abc", token)
    assert e.value.code == 400


# --- 承認 API ---
@pytest.fixture
def srv_r2(tmp_path, audit):
    ran = []
    reg = reg_with()
    reg.register(ToolSpec("danger", "d", {"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": False}, Level.R2,
                          lambda a: ran.append(a) or {"done": a.get("x")}, summarizer=lambda a: f"x={a.get('x')} を実行"))
    agent = Agent(ScriptedLLM([]), reg, audit)
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    store.add("tablet")
    s = make_server(agent, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", token, agent, ran
    s.shutdown()


def post(url, token, body=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), headers={"Authorization": f"Bearer {token}"}, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_approval_endpoints_require_token(srv_r2):
    base, token, agent, ran = srv_r2
    ap = agent.approvals.request("r1", "danger", {"x": 1})
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + f"/api/approvals/{ap['id']}/approve", None, {"hash": ap["hash"]})
    assert e.value.code == 401 and ran == []
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/approvals")
    assert e.value.code == 401


def test_approve_via_api_runs_once_and_second_call_conflicts(srv_r2):
    base, token, agent, ran = srv_r2
    ap = agent.approvals.request("r1", "danger", {"x": 1}, "x=1 を実行")
    listed = json.load(get(base + "/api/approvals", token))["approvals"]
    assert listed[0]["id"] == ap["id"] and listed[0]["summary"] == "x=1 を実行" and listed[0]["status"] == "pending"
    out = json.load(post(base + f"/api/approvals/{ap['id']}/approve", token, {"hash": ap["hash"]}))
    assert out["ok"] and ran == [{"x": 1}]
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + f"/api/approvals/{ap['id']}/approve", "x", {"hash": ap["hash"]})
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + f"/api/approvals/{ap['id']}/approve", token, {"hash": ap["hash"]})
    assert e.value.code == 409 and ran == [{"x": 1}]


@pytest.mark.parametrize("body", [{}, {"hash": "wrong"}, {"hash": ""}])
def test_approve_without_matching_hash_is_refused(srv_r2, body):
    base, token, agent, ran = srv_r2
    ap = agent.approvals.request("r1", "danger", {"x": 1})
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + f"/api/approvals/{ap['id']}/approve", token, body)
    assert e.value.code == 409 and ran == [] and agent.approvals.get(ap["id"])["status"] == "pending"


def test_reject_and_unknown_id_via_api(srv_r2):
    base, token, agent, ran = srv_r2
    ap = agent.approvals.request("r1", "danger", {"x": 1})
    assert json.load(post(base + f"/api/approvals/{ap['id']}/reject", token))["status"] == "rejected"
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/approvals/nope/approve", token, {"hash": "x"})
    assert e.value.code == 404
    assert json.load(get(base + "/api/dashboard", token))["approvals_pending"] == 0


# --- PWA（アプリとしてインストール）用の静的ファイル ---
@pytest.mark.parametrize("path,ctype", [("/manifest.webmanifest", "application/manifest+json"), ("/sw.js", "text/javascript"),
                                        ("/icon-192.png", "image/png"), ("/icon-512.png", "image/png")])
def test_pwa_files_are_served_without_token_with_right_types(srv, path, ctype):
    base, *_ = srv
    r = get(base + path)
    assert r.status == 200 and r.headers["Content-Type"].startswith(ctype) and int(r.headers["Content-Length"]) > 0


def test_manifest_is_installable_shape(srv):
    base, *_ = srv
    m = json.load(get(base + "/manifest.webmanifest"))
    assert m["display"] == "standalone" and m["start_url"] == "/" and m["name"] == "Faraway"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for i in m["icons"]:
        assert get(base + i["src"]).status == 200
    assert get(base + "/icon-512.png").read(8) == b"\x89PNG\r\n\x1a\n"


def test_service_worker_never_caches(srv):
    base, *_ = srv
    sw = get(base + "/sw.js").read().decode()
    assert "caches" not in sw and "cache.put" not in sw.lower()


def test_pages_link_the_manifest(srv):
    base, *_ = srv
    for p in ("/", "/admin"):
        html = get(base + p).read().decode()
        assert 'rel="manifest"' in html and "/manifest.webmanifest" in html


def test_api_still_requires_token_with_static_routes(srv):
    base, *_ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/runs")
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/ui/../config/config.toml")
    assert e.value.code == 404


# --- TLS ---
def test_enable_tls_serves_https_and_rejects_plain_http(tmp_path, audit):
    import shutil, ssl, subprocess
    openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
    if not shutil.which(openssl) and not __import__("os").path.exists(openssl):
        pytest.skip("openssl がない環境")
    crt, key = tmp_path / "t.crt", tmp_path / "t.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    from api.server import enable_tls
    agent = Agent(ScriptedLLM([]), reg_with(), audit)
    store = TokenStore(tmp_path / "t.json")
    store.add("phone")
    s = make_server(agent, store, "127.0.0.1", 0)
    enable_tls(s, str(crt), str(key))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        port = s.server_address[1]
        ctx = ssl.create_default_context(cafile=str(crt))
        r = urllib.request.urlopen(f"https://127.0.0.1:{port}/manifest.webmanifest", context=ctx, timeout=10)
        assert r.status == 200
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    finally:
        s.shutdown()


def test_enable_tls_with_bad_files_raises(tmp_path, audit):
    from api.server import enable_tls
    s = make_server(Agent(ScriptedLLM([]), reg_with(), audit), (lambda st: (st.add("p"), st)[1])(TokenStore(tmp_path / "t.json")), "127.0.0.1", 0)
    with pytest.raises(Exception):
        enable_tls(s, str(tmp_path / "none.crt"), str(tmp_path / "none.key"))
    s.server_close()


def test_admin_update_tab_shows_readable_release_names_not_only_ids(srv):
    base, *_ = srv
    html = get(base + "/admin").read().decode()
    assert "prepared_details" in html and "d.title" in html and "PR #" in html          # PR のタイトルで見せる
    assert "最新（適用できます）" in html and "古い版（適用できません）" in html and "以前の更新で失敗した版" in html   # 現在の版との関係
    assert "迷ったら、「最新」と書かれた 1 つだけ" in html and "d.relation === 'newer'" in html                  # 適用できるのは新しい版だけ
    assert "whiteSpace = 'pre-wrap'" in html                                              # 承認画面の要約の改行を保つ
