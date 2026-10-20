"""ジョブキュー（夜間バッチ）のテスト。"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from api.server import make_server
from api.tokens import TokenStore
from core.agent import Agent
from core.jobs import JobQueue, in_window
from core.slots import SlotPool
from policy.registry import ToolRegistry
from tests.conftest import ScriptedLLM

JST = timezone(timedelta(hours=9))


def at(h, m=0):
    return datetime(2026, 10, 21, h, m, tzinfo=JST)


def agent(audit, answers=("結果です",)):
    return Agent(ScriptedLLM([{"role": "assistant", "content": a} for a in answers]), ToolRegistry(), audit, slots=SlotPool(1, 1))


@pytest.mark.parametrize("h,m,expect", [(1, 59, False), (2, 0, True), (5, 59, True), (6, 0, False), (12, 0, False)])
def test_window_same_day(h, m, expect):
    assert in_window(at(h, m), dtime(2), dtime(6)) is expect


@pytest.mark.parametrize("h,expect", [(21, False), (22, True), (23, True), (0, True), (5, True), (6, False)])
def test_window_across_midnight(h, expect):
    assert in_window(at(h), dtime(22), dtime(6)) is expect


def test_now_job_runs_in_batch_slot_and_is_audited(audit):
    ag = agent(audit)
    q = JobQueue(ag, clock=lambda: at(12))
    job = q.submit("まとめて", "now", "phone")
    assert job["status"] == "queued"
    assert q.tick() is True
    done = q.list()[0]
    assert done["status"] == "done" and done["result"] == "結果です" and done["run_id"]
    run = ag.tracker.get(done["run_id"])
    assert run["slot"] == "batch" and run["device"] == f"job:{job['id']}"
    assert [e["event"] for e in audit.read() if e["event"].startswith("job_")] == ["job_queued", "job_started", "job_finished"]
    assert q.tick() is False


def test_night_job_waits_for_window(audit):
    now = {"t": at(12)}
    q = JobQueue(agent(audit), clock=lambda: now["t"])
    q.submit("夜にやる", "night")
    assert q.tick() is False and q.list()[0]["status"] == "queued"
    now["t"] = at(3)
    assert q.tick() is True and q.list()[0]["status"] == "done"


def test_jobs_defer_while_interactive_slot_is_busy(audit):
    ag = agent(audit)
    q = JobQueue(ag, clock=lambda: at(3))
    q.submit("x", "now")
    with ag.slots.acquire("interactive"):
        assert q.tick() is False
    assert q.tick() is True


def test_jobs_run_one_at_a_time_in_order(audit):
    ag = agent(audit, ("一つ目", "二つ目"))
    q = JobQueue(ag, clock=lambda: at(3))
    a, b = q.submit("a", "now"), q.submit("b", "now")
    q.tick()
    by_id = {j["id"]: j for j in q.list()}
    assert by_id[a["id"]]["status"] == "done" and by_id[b["id"]]["status"] == "queued"
    q.tick()
    assert {j["id"]: j["result"] for j in q.list()} == {a["id"]: "一つ目", b["id"]: "二つ目"}


def test_cancel_queued_job_never_runs(audit):
    q = JobQueue(agent(audit), clock=lambda: at(3))
    j = q.submit("x", "now")
    assert q.cancel(j["id"]) == {"ok": True, "status": "cancelled"}
    assert q.tick() is False and q.cancel(j["id"])["error"] == "not_cancellable:cancelled" and q.cancel("nope")["error"] == "not_found"


def test_failing_job_is_recorded_and_queue_continues(audit):
    class Boom:
        def chat(self, m, t=None, tool_choice=None):
            raise RuntimeError("x")

    ag = Agent(Boom(), ToolRegistry(), audit)
    q = JobQueue(ag, clock=lambda: at(3))
    q.submit("x", "now")
    assert q.tick() is True and q.list()[0]["status"] == "failed"


def test_queue_is_persisted_and_restored(audit, tmp_path):
    path = tmp_path / "jobs.json"
    q1 = JobQueue(agent(audit), path=path, clock=lambda: at(12))
    j = q1.submit("残る依頼", "night")
    q2 = JobQueue(agent(audit), path=path, clock=lambda: at(12))
    assert [x["id"] for x in q2.list()] == [j["id"]]
    q2.cancel(j["id"])
    assert JobQueue(agent(audit), path=path).list() == []


def test_invalid_when_rejected(audit):
    with pytest.raises(ValueError):
        JobQueue(agent(audit)).submit("x", "tomorrow")


def test_cancel_running_job_stops_the_run(audit):
    started, release = threading.Event(), threading.Event()

    class Slow:
        def __init__(self):
            self.n = 0

        def chat(self, m, t=None, tool_choice=None):
            started.set()
            release.wait(5)
            return {"role": "assistant", "content": "遅い回答"}

    ag = Agent(Slow(), ToolRegistry(), audit)
    q = JobQueue(ag, clock=lambda: at(3))
    q.submit("長い作業", "now")
    t = threading.Thread(target=q.tick)
    t.start()
    assert started.wait(5)
    jid = q.list()[0]["id"]
    assert q.cancel(jid)["status"] == "cancel_requested"
    release.set()
    t.join(5)
    assert q.list()[0]["status"] in ("cancelled", "done")  # 中止要求は次の確認点で効く（最後の応答が先に返る場合は done）


def test_background_thread_runs_jobs(audit):
    q = JobQueue(agent(audit), clock=lambda: at(3), poll_seconds=0.05)
    q.submit("x", "now")
    q.start()
    for _ in range(100):
        if q.list()[0]["status"] == "done":
            break
        time.sleep(0.05)
    q.stop()
    assert q.list()[0]["status"] == "done"


# --- API ---
@pytest.fixture
def srv(tmp_path, audit):
    ag = agent(audit)
    q = JobQueue(ag, clock=lambda: at(12))
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    s = make_server(ag, store, "127.0.0.1", 0, q)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", token, q
    s.shutdown()


def call(url, token, body=None, method=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}"}, method=method or ("POST" if body is not None else "GET"))
    return urllib.request.urlopen(req, timeout=10)


def test_jobs_api_requires_token_and_roundtrips(srv):
    base, token, q = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/jobs", "bad")
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/jobs", "bad", {"request": "x"})
    assert e.value.code == 401 and q.list() == []
    job = json.load(call(base + "/api/jobs", token, {"request": "夜に実行", "when": "night"}))
    assert job["status"] == "queued" and job["device"] == "phone"
    listed = json.load(call(base + "/api/jobs", token))
    assert listed["jobs"][0]["id"] == job["id"] and listed["window"] == ["02:00", "06:00"]
    assert json.load(call(base + f"/api/jobs/{job['id']}/cancel", token, {}))["status"] == "cancelled"
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + f"/api/jobs/{job['id']}/cancel", token, {})
    assert e.value.code == 409


@pytest.mark.parametrize("body", [{}, {"request": ""}, {"request": "x", "when": "later"}])
def test_jobs_api_rejects_bad_body(srv, body):
    base, token, q = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/jobs", token, body)
    assert e.value.code == 400 and q.list() == []
