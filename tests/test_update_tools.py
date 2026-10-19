"""自己更新のツール・API・再起動処理のテスト（Updater 本体は test_updater.py。ここでは偽の Updater を使う）。"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from api.server import make_server
from api.tokens import TokenStore
from core import lifecycle
from core.agent import Agent
from core.config import data_path
from core.updater import UpdateError
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call
from tools import update

SHA = "a" * 40


class FakeUpdater:
    def __init__(self):
        self.calls, self.fail = [], False
        self.last_prepare = None

    def version(self):
        return "b" * 40

    def check(self):
        self.calls.append("check")
        return {"state": "behind", "latest": SHA, "current": self.version(), "commits": [{"sha": SHA, "subject": "v2"}]}

    def prepare(self, sha=None):
        self.calls.append(("prepare", sha))
        if self.fail:
            raise UpdateError("テストに失敗しました")
        return {"prepared": SHA[:12], "sha": SHA, "steps": {"tests": "passed"}}

    def prepare_async(self, sha=None):
        self.calls.append(("prepare_async", sha))
        self.last_prepare = {"state": "running"}
        return self.last_prepare

    def apply(self, sha):
        self.calls.append(("apply", sha))
        if self.fail:
            raise UpdateError("検証されていません")
        return {"applied": sha[:12], "restart_requested": True}

    def rollback(self):
        self.calls.append("rollback")
        return {"rolled_back_to": "x", "restart_requested": True}

    def status(self):
        return {"version": self.version(), "supervised": True, "busy": "", "prepared": [SHA[:12]], "remote": "r", "branch": "main", "last_prepare": self.last_prepare}


def test_levels_and_flags():
    t = {s.name: s for s in update.build_tools(FakeUpdater())}
    assert t["update_check"].level is Level.R0 and t["update_prepare"].level is Level.R1
    assert t["update_apply"].level is Level.R2 and t["update_rollback"].level is Level.R2
    assert t["update_check"].egress and t["update_prepare"].egress and not t["update_apply"].egress
    assert "remote_url" not in json.dumps([s.input_schema for s in t.values()])    # 更新元はモデルが指定できない
    assert set(t["update_apply"].input_schema["required"]) == {"sha"}


def test_errors_are_returned_not_raised():
    up = FakeUpdater()
    up.fail = True
    t = {s.name: s for s in update.build_tools(up)}
    assert t["update_prepare"].handler({})["error"] == "update_error" and "テスト" in t["update_prepare"].handler({})["reason"]
    assert t["update_apply"].handler({"sha": SHA})["error"] == "update_error"


def test_apply_goes_through_approval_in_agent(audit):
    up = FakeUpdater()
    reg = ToolRegistry()
    for s in update.build_tools(up):
        reg.register(s)
    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("update_apply", {"sha": SHA})]},
                       {"role": "assistant", "content": "承認待ちです"}])
    ag = Agent(llm, reg, audit, updater=up)
    ag.run("更新して")
    assert up.calls == []                                                    # 承認前は適用されない
    ap = ag.approvals.list()[0]
    assert ap["tool"] == "update_apply" and "bbbbbbbbbbbb → aaaaaaaaaaaa" in ap["summary"] and "自動で元に戻" in ap["summary"]
    assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"]
    assert up.calls == [("apply", SHA)]
    assert not ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"] and len(up.calls) == 1   # 1回限り


def test_failed_apply_is_not_marked_executed(audit):
    up = FakeUpdater()
    up.fail = True
    reg = ToolRegistry()
    for s in update.build_tools(up):
        reg.register(s)
    ag = Agent(ScriptedLLM([]), reg, audit, updater=up)
    ap = ag.approvals.request("r", "update_apply", {"sha": SHA})
    assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["status"] == "failed"


def test_update_tools_are_blocked_after_reading_private_data(audit):
    up = FakeUpdater()
    reg = ToolRegistry()
    for s in update.build_tools(up):
        reg.register(s)
    reg.register(ToolSpec("device_read", "d", {"type": "object", "properties": {}}, Level.R0, lambda a: {"text": "x"}, reads_private=True))
    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("device_read", {}, "c1")]},
                       {"role": "assistant", "content": "", "tool_calls": [tool_call("update_check", {}, "c2")]},
                       {"role": "assistant", "content": "ok"}])
    r = Agent(llm, reg, audit, updater=up).run("読んでから更新確認")
    assert [t["status"] for t in r.trace] == ["ok", "egress_blocked"] and up.calls == []


def test_registry_includes_update_tools_only_when_configured(tmp_path, audit):
    from core.app import build_registry
    from core.bridge import DeviceBridge
    cfg = {"llm": {"endpoints": []}, "search": {"index_path": str(tmp_path / "i.sqlite3")}}
    assert not any(n.startswith("update_") for n in build_registry(cfg, audit, DeviceBridge()).names())
    names = build_registry(cfg, audit, DeviceBridge(), FakeUpdater()).names()
    assert {"update_check", "update_prepare", "update_apply", "update_rollback"} <= set(names)


# --- lifecycle ---
def test_request_restart_needs_supervisor_and_handler(monkeypatch):
    monkeypatch.setattr(lifecycle, "_callback", None)
    monkeypatch.delenv("FARAWAY_SUPERVISED", raising=False)
    assert lifecycle.request_restart(0) is False and lifecycle.supervised() is False
    hit = threading.Event()
    lifecycle.set_restart_handler(hit.set)
    assert lifecycle.request_restart(0) is False                             # スーパーバイザー経由でなければ再起動しない
    monkeypatch.setenv("FARAWAY_SUPERVISED", "1")
    assert lifecycle.request_restart(0.05) is True and hit.wait(2)
    monkeypatch.setattr(lifecycle, "_callback", None)


def test_data_path(monkeypatch, tmp_path):
    monkeypatch.delenv("FARAWAY_DATA_DIR", raising=False)
    assert str(data_path("logs/audit")) == "logs\\audit" or str(data_path("logs/audit")) == "logs/audit"
    monkeypatch.setenv("FARAWAY_DATA_DIR", str(tmp_path))
    assert data_path("logs/audit") == tmp_path / "logs" / "audit"
    absolute = tmp_path / "abs"
    assert data_path(str(absolute)) == absolute


# --- API ---
@pytest.fixture
def srv(tmp_path, audit):
    up = FakeUpdater()
    reg = ToolRegistry()
    for s in update.build_tools(up):
        reg.register(s)
    ag = Agent(ScriptedLLM([]), reg, audit, updater=up)
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    s = make_server(ag, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", token, ag, up
    s.shutdown()


def call(url, token, body=None):
    r = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}"},
                               method="POST" if body is not None else "GET")
    resp = urllib.request.urlopen(r, timeout=10)
    return resp.status, json.load(resp)


def test_update_api_requires_token(srv):
    base, token, ag, up = srv
    for path, body in (("/api/update/status", None), ("/api/update/check", {}), ("/api/update/prepare", {}), ("/api/update/apply", {"sha": SHA}), ("/api/update/rollback", {})):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base + path, "bad", body)
        assert e.value.code == 401
    assert up.calls == [] and ag.approvals.list() == []


def test_status_check_and_async_prepare(srv):
    base, token, ag, up = srv
    assert call(base + "/api/update/status", token)[1]["prepared"] == [SHA[:12]]
    assert call(base + "/api/update/check", token, {})[1]["state"] == "behind"
    code, body = call(base + "/api/update/prepare", token, {"sha": SHA})
    assert code == 202 and body["state"] == "running" and ("prepare_async", SHA) in up.calls


def test_apply_and_rollback_buttons_only_create_approvals(srv, audit):
    base, token, ag, up = srv
    _, a = call(base + "/api/update/apply", token, {"sha": SHA})
    _, r = call(base + "/api/update/rollback", token, {})
    assert up.calls == []                                                    # 画面のボタンでも、承認なしには何も実行されない
    pending = {x["tool"]: x for x in ag.approvals.list()}
    assert pending["update_apply"]["args"] == {"sha": SHA} and pending["update_apply"]["id"] == a["approval_id"]
    assert pending["update_rollback"]["id"] == r["approval_id"]
    out = ag.approvals.approve(a["approval_id"], a["hash"], "phone")
    assert out["ok"] and up.calls == [("apply", SHA)]


def test_update_api_when_disabled(tmp_path, audit):
    ag = Agent(ScriptedLLM([]), ToolRegistry(), audit)
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    s = make_server(ag, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{s.server_address[1]}"
        assert call(base + "/api/update/status", token)[1] == {"enabled": False}
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base + "/api/update/check", token, {})
        assert e.value.code == 404
    finally:
        s.shutdown()


def test_update_error_becomes_409(srv):
    base, token, ag, up = srv
    ag.updater.check = lambda: (_ for _ in ()).throw(UpdateError("更新元に接続できません"))
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/update/check", token, {})
    assert e.value.code == 409
