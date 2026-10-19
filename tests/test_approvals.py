"""R2 承認フローのテスト: 承認なしで実行されない・別の操作に流用できない・1回限り・期限切れ・監査。"""
from __future__ import annotations

import threading
import time

import pytest

from core.agent import Agent
from core.approvals import ApprovalManager, operation_hash
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call

S = {"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": False}


class Env:
    def __init__(self, audit, ttl=600.0):
        self.ran: list = []
        self.now = [1000.0]
        self.reg = ToolRegistry()
        self.reg.register(ToolSpec("danger", "d", S, Level.R2, lambda a: self.ran.append(dict(a)) or {"done": a.get("x")},
                                   summarizer=lambda a: f"danger を x={a.get('x')} で実行"))
        self.reg.register(ToolSpec("safe", "d", S, Level.R0, lambda a: {"ok": 1}))
        self.m = ApprovalManager(audit, self.reg, ttl=ttl, clock=lambda: self.now[0])


@pytest.fixture
def env(audit):
    return Env(audit)


def events(audit, name):
    return [e for e in audit.read() if e["event"] == name]


def test_request_does_not_execute_and_is_audited(env, audit):
    ap = env.m.request("r1", "danger", {"x": 1}, "x=1 を実行")
    assert env.ran == [] and ap["status"] == "pending" and ap["hash"] == operation_hash("danger", {"x": 1})
    assert ap["expires_at"] == 1600.0 and events(audit, "approval_requested")[0]["op_hash"] == ap["hash"]


def test_approve_executes_exactly_the_requested_operation_once(env, audit):
    ap = env.m.request("r1", "danger", {"x": 1})
    out = env.m.approve(ap["id"], ap["hash"], "phone")
    assert out["ok"] and out["status"] == "executed" and env.ran == [{"x": 1}]
    again = env.m.approve(ap["id"], ap["hash"], "phone")
    assert not again["ok"] and again["error"] == "not_pending:executed" and env.ran == [{"x": 1}]
    assert events(audit, "approval_granted")[0]["device"] == "phone"
    assert events(audit, "approved_execution")[0]["status"] == "executed"


def test_two_devices_approving_simultaneously_run_once(env):
    gate = threading.Event()
    calls = []

    def slow(a):
        calls.append(1)
        gate.wait(2)
        return {"ok": True}

    env.reg._tools["danger"] = ToolSpec("danger", "d", S, Level.R2, slow)
    ap = env.m.request("r1", "danger", {"x": 1})
    results = []
    ts = [threading.Thread(target=lambda d=d: results.append(env.m.approve(ap["id"], ap["hash"], d))) for d in ("pc", "phone", "tablet")]
    for t in ts:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join(5)
    assert len(calls) == 1 and sum(1 for r in results if r["ok"]) == 1
    assert all(r["error"].startswith("not_pending") for r in results if not r["ok"])


def test_approval_cannot_be_reused_for_another_operation(env):
    a = env.m.request("r1", "danger", {"x": 1})
    b = env.m.request("r1", "danger", {"x": 2})
    assert a["hash"] != b["hash"]
    out = env.m.approve(b["id"], a["hash"], "phone")  # A の承認（ハッシュ）を B に使う
    assert not out["ok"] and out["error"] == "hash_mismatch" and env.ran == []
    assert env.m.approve(a["id"], a["hash"], "phone")["ok"] and env.ran == [{"x": 1}]  # B は実行されていない
    assert env.m.get(b["id"])["status"] == "pending"


def test_tampered_args_after_request_are_refused(env, audit):
    ap = env.m.request("r1", "danger", {"x": 1})
    env.m._items[ap["id"]]["args"]["x"] = 999  # 保存された引数が差し替えられた場合
    out = env.m.approve(ap["id"], ap["hash"], "phone")
    assert out["error"] == "hash_mismatch" and env.ran == []
    assert events(audit, "approval_hash_mismatch")


def test_expired_request_cannot_be_approved(env, audit):
    ap = env.m.request("r1", "danger", {"x": 1})
    env.now[0] += 601
    out = env.m.approve(ap["id"], ap["hash"], "phone")
    assert not out["ok"] and out["error"] == "not_pending:expired" and env.ran == []
    assert len(events(audit, "approval_expired")) == 1
    env.m.list()
    assert len(events(audit, "approval_expired")) == 1  # 記録は1回だけ


def test_not_expired_just_before_deadline(env):
    ap = env.m.request("r1", "danger", {"x": 1})
    env.now[0] += 599
    assert env.m.approve(ap["id"], ap["hash"], "phone")["ok"]


def test_reject_blocks_later_approval(env, audit):
    ap = env.m.request("r1", "danger", {"x": 1})
    assert env.m.reject(ap["id"], "phone")["ok"]
    assert env.m.approve(ap["id"], ap["hash"], "phone")["error"] == "not_pending:rejected" and env.ran == []
    assert events(audit, "approval_rejected")[0]["device"] == "phone"


def test_identical_pending_requests_are_deduplicated_but_new_after_decision(env):
    a = env.m.request("r1", "danger", {"x": 1})
    assert env.m.request("r2", "danger", {"x": 1})["id"] == a["id"]
    env.m.reject(a["id"], "phone")
    assert env.m.request("r3", "danger", {"x": 1})["id"] != a["id"]


def test_hash_ignores_key_order():
    assert operation_hash("t", {"a": 1, "b": 2}) == operation_hash("t", {"b": 2, "a": 1}) != operation_hash("t", {"a": 1, "b": 3})


def test_unknown_missing_and_non_r2_tools(env):
    assert env.m.approve("nope", "h", "d")["error"] == "not_found" and env.m.reject("nope", "d")["error"] == "not_found"
    ap = env.m.request("r1", "safe", {})  # R2 ではないツールの要求は実行しない
    assert env.m.approve(ap["id"], ap["hash"], "phone")["error"] == "tool_not_available"


def test_handler_failure_is_recorded_not_raised(env, audit):
    def boom(a):
        raise RuntimeError("壊れた")

    env.reg._tools["danger"] = ToolSpec("danger", "d", S, Level.R2, boom)
    ap = env.m.request("r1", "danger", {"x": 1})
    out = env.m.approve(ap["id"], ap["hash"], "phone")
    assert out["status"] == "failed" and env.m.get(ap["id"])["status"] == "failed"
    assert events(audit, "approved_execution")[0]["status"] == "failed"


def test_pending_count(env):
    env.m.request("r1", "danger", {"x": 1})
    env.m.request("r1", "danger", {"x": 2})
    assert env.m.pending_count() == 2
    env.now[0] += 700
    assert env.m.pending_count() == 0


def test_agent_creates_request_and_never_executes_r2(audit):
    reg = Env(audit).reg
    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("danger", {"x": 5})]},
                       {"role": "assistant", "content": "承認待ちです"}])
    ran = []
    reg._tools["danger"] = ToolSpec("danger", "d", S, Level.R2, lambda a: ran.append(a), summarizer=lambda a: "x=5 を実行")
    ag = Agent(llm, reg, audit)
    events_seen = []
    ag.run("実行して", on_event=events_seen.append)
    assert ran == []
    pending = ag.approvals.list()
    assert len(pending) == 1 and pending[0]["summary"] == "x=5 を実行" and pending[0]["args"] == {"x": 5}
    assert any(e["type"] == "approval" and e["approval_id"] == pending[0]["id"] for e in events_seen)
    tool_msg = llm.seen[1][-1]["content"]
    assert pending[0]["id"] in tool_msg and "実行したと言ってはいけません" in tool_msg
    ag.approvals.approve(pending[0]["id"], pending[0]["hash"], "phone")
    assert ran == [{"x": 5}]
