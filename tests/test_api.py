import json
import threading
import urllib.error
import urllib.request

import pytest

from api.server import make_server
from api.tokens import TokenStore
from core.agent import Agent
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call


@pytest.fixture
def srv(tmp_path, audit):
    reg = ToolRegistry()
    reg.register(ToolSpec("host_status", "d", {"type": "object", "properties": {}}, Level.R0, lambda a: {"gpu": "mock"}))
    agent = Agent(ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("host_status", {})]},
                               {"role": "assistant", "content": "GPUは正常です"}]), reg, audit)
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    s = make_server(agent, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", token, store, audit
    s.shutdown()


def call(url, token=None, body=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h)
    return urllib.request.urlopen(req, timeout=10)


def test_refuses_to_start_without_tokens(tmp_path, audit):
    with pytest.raises(RuntimeError):
        make_server(Agent(ScriptedLLM([]), ToolRegistry(), audit), TokenStore(tmp_path / "none.json"), "127.0.0.1", 0)


def test_api_requires_token(srv):
    base, token, _, audit = srv
    for t in (None, "wrong"):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base + "/api/health", t)
        assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/chat", None, {"request": "x"})
    assert e.value.code == 401
    assert call(base + "/api/health", token).status == 200
    assert any(x["event"] == "auth_failed" for x in audit.read())
    assert "wrong" not in json.dumps(audit.read())


def test_chat_streams_events_and_audits(srv):
    base, token, _, audit = srv
    body = call(base + "/api/chat", token, {"request": "GPUの状態は？"}).read().decode()
    evs = [json.loads(l[6:]) for l in body.split("\n\n") if l.startswith("data: ")]
    assert [e["type"] for e in evs] == ["tool", "message", "done"]
    assert evs[1]["text"] == "GPUは正常です"
    assert any(e["event"] == "tool_call" and e["tool"] == "host_status" for e in audit.read())


def test_revoked_device_loses_access(srv):
    base, token, store, _ = srv
    store.revoke("phone")
    with pytest.raises(urllib.error.HTTPError):
        call(base + "/api/health", token)


def test_tokens_stored_as_hash(srv):
    _, token, store, _ = srv
    assert token not in store.path.read_text()


def test_ui_page_served_without_token(srv):
    base, *_ = srv
    assert "アシスタント" in call(base + "/").read().decode()


def test_bad_request_body(srv):
    base, token, *_ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/chat", token, {"nope": 1})
    assert e.value.code == 400
