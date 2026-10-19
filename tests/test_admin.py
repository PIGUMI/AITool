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
