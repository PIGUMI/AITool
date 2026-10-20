"""自己更新（core/updater.py）のテスト。ローカルの bare リポジトリを「更新元」にして、本物の git で検証する。"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from core.audit import AuditLog
from core.updater import UpdateError, Updater, bootstrap

ID = ["-c", "user.name=t", "-c", "user.email=t@e.com"]

SELFCHECK = '''
import sys
from pathlib import Path
sys.exit(1 if Path("FAIL_SELFCHECK").exists() else 0)
'''
CHECK_TESTS = '''
import os, sys, time
from pathlib import Path
if Path("SLOW").exists():
    time.sleep(2)
if "MY_SECRET_API_KEY" in os.environ:
    print("secret leaked into test env"); sys.exit(2)
sys.exit(1 if Path("FAIL_TESTS").exists() else 0)
'''


def git(cwd, *a):
    return subprocess.run(["git", *ID, *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, files: dict[str, str], msg: str, remote_branch: str = "main") -> str:
    for name, text in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", msg)
    git(work, "push", "-q", "origin", f"HEAD:{remote_branch}")
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(bare), str(work))
    git(work, "checkout", "-q", "-b", "main")
    v1 = commit(work, {"core/__init__.py": "", "core/selfcheck.py": SELFCHECK, "check_tests.py": CHECK_TESTS, "app.txt": "v1", "core/supervisor.py": "# supervisor",
                       "config/config.toml": "[api]\nport = 1\n"}, "v1")
    inst = tmp_path / "install"
    audit = AuditLog(tmp_path / "audit")
    state = {"version": v1, "supervised": True, "restarts": 0, "restart_ok": True}

    def restart():
        state["restarts"] += 1
        return state["restart_ok"]

    def make(**cfg):
        c = {"remote_url": str(bare), "branch": "main", "test_command": ["python", "check_tests.py"], "build_exe": False, **cfg}
        return Updater(c, audit, install_dir=inst, version=lambda: state["version"], restart=restart, supervised=lambda: state["supervised"])

    return {"bare": bare, "work": work, "inst": inst, "v1": v1, "make": make, "state": state, "audit": audit, "tmp": tmp_path}


# --- check ---
def test_check_up_to_date_and_pinned_origin(world):
    up = world["make"]()
    info = up.check()
    assert info["state"] == "up_to_date" and info["latest"] == world["v1"] and info["commits"] == []
    assert any(e["event"] == "update_checked" for e in world["audit"].read())


def test_check_behind_lists_commits(world):
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2: 機能追加")
    info = world["make"]().check()
    assert info["state"] == "behind" and info["latest"] == v2
    assert [c["subject"] for c in info["commits"]] == ["v2: 機能追加"] and "マージ済み" in info["note"]


def test_check_ahead_diverged_and_unknown(world):
    up = world["make"]()
    up.check()
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2")
    up.check()                                                              # v2 を update 用 clone に取り込む（現在の版として知っている状態）
    world["state"]["version"] = v2
    git(world["work"], "reset", "-q", "--hard", world["v1"])
    git(world["work"], "push", "-q", "-f", "origin", "HEAD:main")           # 更新元を巻き戻した（手元のほうが新しい）
    assert up.check()["state"] == "ahead"
    v2b = commit(world["work"], {"app.txt": "other"}, "別の変更")           # 手元 v2 と履歴が分岐
    assert up.check()["state"] == "diverged"
    world["state"]["version"] = "unknown"
    assert up.check()["state"] == "current_unknown"
    world["state"]["version"] = "f" * 40                                    # 更新元に存在しない版
    assert up.check()["state"] == "current_unknown"


def test_origin_mismatch_is_refused(world, tmp_path):
    up = world["make"]()
    up.check()
    git(up.repo, "remote", "set-url", "origin", str(tmp_path / "evil.git"))
    with pytest.raises(UpdateError, match="origin"):
        up.check()


def test_no_remote_configured():
    with pytest.raises(UpdateError, match="remote_url"):
        Updater({}).check()


# --- prepare ---
def test_prepare_extracts_verifies_and_marks(world):
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2")
    out = world["make"]().prepare()
    name = v2[:12]
    rel = world["inst"] / "releases" / name
    assert out["prepared"] == name and out["steps"]["selfcheck"] == "passed" and out["steps"]["tests"] == "passed"
    assert (rel / "app.txt").read_text() == "v2" and (rel / "RELEASE").read_text() == v2
    v = json.loads((rel / "VERIFIED").read_text())
    assert v["sha"] == v2 and not (rel / ".git").exists()
    assert not any(p.name.startswith(".tmp-") for p in (world["inst"] / "releases").iterdir())
    assert any(e["event"] == "update_prepared" for e in world["audit"].read())


def test_prepare_twice_reports_already(world):
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2")
    up = world["make"]()
    up.prepare()
    assert up.prepare(v2)["already"] is True


@pytest.mark.parametrize("marker,step", [("FAIL_TESTS", "テスト"), ("FAIL_SELFCHECK", "自己検査")])
def test_failed_verification_leaves_nothing_and_never_applies(world, marker, step):
    commit(world["work"], {marker: "1", "app.txt": "broken"}, "壊れた版")
    up = world["make"]()
    with pytest.raises(UpdateError, match=step):
        up.prepare()
    assert up.prepared() == [] and not any((world["inst"] / "releases").iterdir())
    assert list((world["inst"] / "logs").glob("prepare-*.log"))
    assert any(e["event"] == "update_prepare_failed" for e in world["audit"].read())


def test_tests_run_without_real_secrets_in_env(world, monkeypatch):
    monkeypatch.setenv("MY_SECRET_API_KEY", "leakme")
    commit(world["work"], {"app.txt": "v2"}, "v2")
    assert world["make"]().prepare()["steps"]["tests"] == "passed"   # CHECK_TESTS は環境変数の漏れを検知すると失敗する


def test_prepare_refuses_non_branch_ahead_bad_and_malformed(world):
    up = world["make"]()
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2")
    git(world["work"], "checkout", "-q", "-b", "side")
    side = commit(world["work"], {"side.txt": "x"}, "未マージの変更", remote_branch="side")   # main には無い
    with pytest.raises(UpdateError, match="マージ済み"):
        up.prepare(side)
    for bad in ("zzz", "../x", "HEAD", "12"):
        with pytest.raises(UpdateError, match="16進数"):
            up.prepare(bad)
    (world["inst"] / "bad.txt").write_text(v2[:12] + "\n")
    with pytest.raises(UpdateError, match="再試行しません"):
        up.prepare()
    (world["inst"] / "bad.txt").unlink()
    world["state"]["version"] = v2
    with pytest.raises(UpdateError, match="すでにその版"):
        up.prepare()


def test_prepare_refuses_when_current_is_ahead(world):
    up = world["make"]()
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2")
    up.check()                                                              # v2 を知っている状態にしてから、更新元を巻き戻す
    world["state"]["version"] = v2
    git(world["work"], "reset", "-q", "--hard", world["v1"])
    git(world["work"], "push", "-q", "-f", "origin", "HEAD:main")
    with pytest.raises(UpdateError, match="自動では更新できません"):
        up.prepare()


def test_only_one_prepare_at_a_time(world):
    commit(world["work"], {"SLOW": "1", "app.txt": "v2"}, "遅い版")
    up = world["make"]()
    t = threading.Thread(target=up.prepare)
    t.start()
    for _ in range(100):
        if "準備中" in up.busy:
            break
        time.sleep(0.05)
    with pytest.raises(UpdateError, match="進行中"):
        up.prepare()
    t.join(30)


def test_zip_slip_is_rejected(world, tmp_path):
    up = world["make"]()
    up.check()
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escaped.txt", "pwn")
    real_git = up._git

    def fake_git(*args, **kw):
        if args[0] == "archive":
            Path(args[args.index("-o") + 1]).write_bytes(evil.read_bytes())
            return ""
        return real_git(*args, **kw)

    up._git = fake_git
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(UpdateError, match="不正なパス"):
        up._extract("a" * 40, dest)
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path.parent / "escaped.txt").exists()


# --- apply / rollback ---
def prepared_world(world):
    """現在の版 v1 をリリースとして置き、v2 を準備済みにする。"""
    cur = world["v1"][:12]
    (world["inst"] / "releases" / cur).mkdir(parents=True)
    (world["inst"] / "current.txt").write_text(cur)
    v2 = commit(world["work"], {"app.txt": "v2"}, "v2")
    up = world["make"]()
    up.prepare()
    return up, cur, v2


def test_apply_switches_pointers_and_requests_restart(world):
    up, cur, v2 = prepared_world(world)
    out = up.apply(v2)
    assert out["applied"] == v2[:12] and out["previous"] == cur and world["state"]["restarts"] == 1
    assert (world["inst"] / "current.txt").read_text() == v2[:12] and (world["inst"] / "previous.txt").read_text() == cur
    pend = json.loads((world["inst"] / "pending.json").read_text())
    assert pend["from"] == cur and pend["to"] == v2[:12]
    assert any(e["event"] == "update_applied" for e in world["audit"].read())


def test_apply_requires_supervisor_verification_and_known_current(world):
    up, cur, v2 = prepared_world(world)
    world["state"]["supervised"] = False
    with pytest.raises(UpdateError, match="スーパーバイザー"):
        up.apply(v2)
    world["state"]["supervised"] = True
    with pytest.raises(UpdateError, match="検証されていません"):
        up.apply("a" * 40)                                                   # 準備していない版
    with pytest.raises(UpdateError, match="sha を指定"):
        up.apply("")
    (world["inst"] / "current.txt").write_text("nonexistent")
    with pytest.raises(UpdateError, match="リリースとして保存されていません"):
        up.apply(v2)
    assert world["state"]["restarts"] == 0 and (world["inst"] / "current.txt").read_text() == "nonexistent"


def test_apply_refuses_bad_and_same_version(world):
    up, cur, v2 = prepared_world(world)
    (world["inst"] / "bad.txt").write_text(v2[:12] + "\n")
    with pytest.raises(UpdateError, match="再試行しません"):
        up.apply(v2)
    (world["inst"] / "bad.txt").unlink()
    (world["inst"] / "current.txt").write_text(v2[:12])
    (world["inst"] / "releases" / v2[:12]).mkdir(exist_ok=True)
    with pytest.raises(UpdateError, match="すでにその版"):
        up.apply(v2)


def test_apply_reverts_when_restart_cannot_be_requested(world):
    up, cur, v2 = prepared_world(world)
    world["state"]["restart_ok"] = False
    with pytest.raises(UpdateError, match="取り消しました"):
        up.apply(v2)
    assert (world["inst"] / "current.txt").read_text() == cur and not (world["inst"] / "pending.json").exists()


def test_manual_rollback(world):
    up, cur, v2 = prepared_world(world)
    up.apply(v2)
    out = up.rollback()
    assert out["rolled_back_to"] == cur and (world["inst"] / "current.txt").read_text() == cur
    assert (world["inst"] / "previous.txt").read_text() == v2[:12] and not (world["inst"] / "pending.json").exists()
    assert json.loads((world["inst"] / "last_update.json").read_text())["status"] == "manual_rollback"
    world["state"]["supervised"] = False
    with pytest.raises(UpdateError):
        up.rollback()


def test_rollback_without_previous(world):
    with pytest.raises(UpdateError, match="戻る先"):
        world["make"]().rollback()


def test_old_releases_are_pruned_but_current_and_previous_stay(world):
    up, cur, v2 = prepared_world(world)
    rel = world["inst"] / "releases"
    for i, n in enumerate(("old1", "old2", "old3", "old4")):
        (rel / n).mkdir()
        (rel / n / "x").write_text(n)
        import os
        os.utime(rel / n, (time.time() - 1000 + i, time.time() - 1000 + i))
    up.apply(v2)
    left = {p.name for p in rel.iterdir()}
    assert {cur, v2[:12]} <= left and len(left) <= 3


def test_status_summary(world):
    up, cur, v2 = prepared_world(world)
    up.apply(v2)
    st = up.status()
    assert st["supervised"] and st["current_release"] == v2[:12] and st["previous_release"] == cur and st["pending"]["to"] == v2[:12]
    assert st["remote"] == str(world["bare"]) and st["branch"] == "main"


# --- bootstrap ---
def test_bootstrap_imports_head_and_installs_supervisor_and_config(world, tmp_path):
    inst = tmp_path / "boot"
    out = bootstrap(world["work"], inst)
    name = world["v1"][:12]
    assert out["release"] == name and (inst / "current.txt").read_text() == name
    assert (inst / "releases" / name / "RELEASE").read_text() == world["v1"] and (inst / "releases" / name / "VERIFIED").is_file()
    assert (inst / "supervisor.py").read_text() == "# supervisor" and (inst / "config.toml").read_text() == "[api]\nport = 1\n"
    assert out["config_copied"] is True and out["dirty_checkout"] is False
    (inst / "config.toml").write_text("[api]\nport = 9\n")                   # 設定は上書きしない
    again = bootstrap(world["work"], inst)
    assert again["config_copied"] is False and (inst / "config.toml").read_text() == "[api]\nport = 9\n"


def test_bootstrap_reports_dirty_checkout(world, tmp_path):
    (world["work"] / "uncommitted.txt").write_text("x")
    assert bootstrap(world["work"], tmp_path / "boot2")["dirty_checkout"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 用のビルド手順")
def test_exe_build_runs_the_release_script_with_a_process_scoped_policy_bypass(world):
    """環境を絞った実行でも、リリース自身の build.ps1 が実行ポリシーで止まらないこと（実機で見つかった不具合の再発防止）。"""
    commit(world["work"], {"apps/windows/build.ps1": "# build", "app.txt": "v2"}, "v2 with build script")
    calls = []
    real = subprocess.run

    def fake_run(cmd, **kw):
        if cmd and cmd[0] == "powershell":
            calls.append(cmd)
            Path(kw["cwd"], "dist").mkdir(exist_ok=True)
            Path(kw["cwd"], "dist", "Faraway.exe").write_bytes(b"MZ")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, **kw)

    up = world["make"](build_exe=True)
    up.run = fake_run
    out = up.prepare()
    assert out["steps"]["exe"] == "built"
    assert calls and "-ExecutionPolicy" in calls[0] and calls[0][calls[0].index("-ExecutionPolicy") + 1] == "Bypass"
    assert calls[0][-2] == "-File" and calls[0][-1].endswith("build.ps1")
