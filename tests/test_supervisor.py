"""スーパーバイザー（見張り役）のテスト。本物のサーバーの代わりに、振る舞いを切り替えられる偽サーバーを起動する。"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from core.supervisor import RESTART_EXIT_CODE, Supervisor

FAKE = '''
import http.server, os, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
mode = (here / "mode.txt").read_text().strip()
if mode == "crash":
    sys.exit(1)
if mode == "nohttp":
    time.sleep(60)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401); self.end_headers()
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["FAKE_PORT"])), H)
srv.timeout = 0.2
(here / "started.txt").write_text(os.environ.get("FARAWAY_SUPERVISED", "") + "|" + os.environ.get("FARAWAY_INSTALL_DIR", ""))
while True:
    srv.handle_request()
    if (here / "RESTART").exists():
        sys.exit(75)
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_release(install: Path, name: str, mode: str = "ok") -> Path:
    d = install / "releases" / name
    d.mkdir(parents=True)
    (d / "fake_server.py").write_text(FAKE, encoding="utf-8")
    (d / "mode.txt").write_text(mode, encoding="utf-8")
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("FAKE_PORT", str(port))
    install = tmp_path / "inst"
    install.mkdir()
    logs: list[str] = []

    def sup(**kw):
        params = dict(server_cmd=[sys.executable, "fake_server.py"], health_url=f"http://127.0.0.1:{port}/api/health",
                      stable_seconds=1.0, health_deadline=4.0, backoff=0.05, crash_window=30.0, max_crashes=3, log=logs.append)
        params.update(kw)
        return Supervisor(install, **params)

    return install, sup, logs, port


def start(sup: Supervisor):
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(code=sup.run()), daemon=True)
    t.start()
    return t, result


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def stop(sup, t):
    sup.stop_requested = True
    t.join(20)


def test_starts_current_release_with_supervised_env(env):
    install, mk, logs, port = env
    rel = make_release(install, "aaa111")
    (install / "current.txt").write_text("aaa111")
    sup = mk()
    t, res = start(sup)
    try:
        assert wait_for(lambda: (rel / "started.txt").exists() and sup.healthy())
        assert (rel / "started.txt").read_text() == f"1|{install}"          # スーパーバイザー経由であること・インストール先が渡る
    finally:
        stop(sup, t)
    assert res["code"] == 0


def test_missing_current_release_stops_with_error(env):
    install, mk, *_ = env
    (install / "current.txt").write_text("nothere")
    assert mk().run() == 2


def test_update_restart_switches_to_new_release_and_confirms(env):
    install, mk, logs, port = env
    a = make_release(install, "aaa111")
    (install / "current.txt").write_text("aaa111")
    sup = mk()
    t, res = start(sup)
    try:
        assert wait_for(lambda: (a / "started.txt").exists() and sup.healthy())
        b = make_release(install, "bbb222")                                  # 更新の「準備」が済んだ新しい版
        (install / "previous.txt").write_text("aaa111")
        (install / "current.txt").write_text("bbb222")
        (install / "pending.json").write_text(json.dumps({"from": "aaa111", "to": "bbb222"}))
        (a / "RESTART").write_text("1")                                       # 旧版が「更新の適用」で終了コード 75 を返す
        assert wait_for(lambda: (b / "started.txt").exists())
        assert wait_for(lambda: json.loads((install / "last_update.json").read_text()).get("status") == "applied" if (install / "last_update.json").exists() else False)
        assert not (install / "pending.json").exists() and not (install / "bad.txt").exists()
        assert (install / "current.txt").read_text() == "bbb222"
    finally:
        stop(sup, t)


def test_new_release_that_crashes_is_rolled_back(env):
    install, mk, logs, port = env
    a = make_release(install, "aaa111")
    make_release(install, "bbb222", mode="crash")
    (install / "previous.txt").write_text("aaa111")
    (install / "current.txt").write_text("bbb222")
    (install / "pending.json").write_text(json.dumps({"from": "aaa111", "to": "bbb222"}))
    sup = mk()
    t, res = start(sup)
    try:
        assert wait_for(lambda: (a / "started.txt").exists() and sup.healthy())      # 元の版で動いている
        assert (install / "current.txt").read_text() == "aaa111"
        assert (install / "bad.txt").read_text().split() == ["bbb222"]               # 失敗した版は記録され、再試行されない
        last = json.loads((install / "last_update.json").read_text())
        assert last["status"] == "rolled_back" and last["to"] == "bbb222" and "試用期間中に終了" in last["reason"]
        assert not (install / "pending.json").exists()
    finally:
        stop(sup, t)


def test_new_release_that_never_answers_health_check_is_rolled_back(env):
    install, mk, logs, port = env
    a = make_release(install, "aaa111")
    make_release(install, "bbb222", mode="nohttp")
    (install / "previous.txt").write_text("aaa111")
    (install / "current.txt").write_text("bbb222")
    (install / "pending.json").write_text(json.dumps({"from": "aaa111", "to": "bbb222"}))
    sup = mk(health_deadline=2.5)
    t, res = start(sup)
    try:
        assert wait_for(lambda: (a / "started.txt").exists() and sup.healthy(), timeout=20)
        last = json.loads((install / "last_update.json").read_text())
        assert last["status"] == "rolled_back" and "ヘルスチェック" in last["reason"]
        assert (install / "current.txt").read_text() == "aaa111"
    finally:
        stop(sup, t)


def test_rollback_fails_cleanly_without_previous(env):
    install, mk, logs, port = env
    make_release(install, "bbb222", mode="crash")
    (install / "current.txt").write_text("bbb222")
    (install / "pending.json").write_text(json.dumps({"from": "", "to": "bbb222"}))
    sup = mk()
    result, _ = sup.run_once()
    assert result == "rolled_back"
    assert json.loads((install / "last_update.json").read_text())["status"] == "rollback_failed"


def test_crash_loop_gives_up(env):
    install, mk, logs, port = env
    make_release(install, "aaa111", mode="crash")
    (install / "current.txt").write_text("aaa111")
    assert mk(max_crashes=3).run() == 1
    assert sum("異常終了" in m for m in logs) == 3


def test_normal_crash_is_restarted(env):
    install, mk, logs, port = env
    a = make_release(install, "aaa111", mode="crash")
    (install / "current.txt").write_text("aaa111")
    sup = mk(max_crashes=50, backoff=0.2)
    t, res = start(sup)
    try:
        assert wait_for(lambda: sum("起動:" in m for m in logs) >= 3)                # 落ちても起動し直す
        (a / "mode.txt").write_text("ok")                                            # 直ったら、そのまま動き続ける
        assert wait_for(lambda: sup.healthy())
    finally:
        stop(sup, t)


def test_restart_exit_code_is_75():
    assert RESTART_EXIT_CODE == 75
    from core.lifecycle import RESTART_EXIT_CODE as LIFECYCLE_CODE
    assert LIFECYCLE_CODE == 75


def test_state_files_survive_supervisor_restart(env):
    install, mk, *_ = env
    make_release(install, "aaa111")
    (install / "current.txt").write_text("aaa111")
    (install / "previous.txt").write_text("old000")
    s1 = mk()
    assert s1.current() == "aaa111"
    s2 = mk()
    assert s2.current() == "aaa111" and (install / "previous.txt").read_text() == "old000"
