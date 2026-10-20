"""dev ツールのテスト。ローカルの bare リポジトリを「リモート」にして、本物の git で検証する（ネットワーク不要）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agent import Agent
from policy.levels import Level
from policy.registry import ToolRegistry
from tests.conftest import ScriptedLLM, tool_call
from tools.dev import Dev, build_tools, is_secret_path, safe_path

ID = ["-c", "user.name=t", "-c", "user.email=t@e.com"]


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *ID, *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (seed / "README.md").write_text("# demo\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "main")
    return bare


def make(tmp_path, remote, **extra):
    repo = {"name": "proj", "remote_url": str(remote), "base_ref": "origin/main",
            "test": [sys.executable, "-c", "import sys; sys.exit(1)"], "build": [sys.executable, "-c", "print('built')"], **extra}
    cfg = {"work_dir": str(tmp_path / "work"), "repos": [repo], "max_fix_attempts": 5, "require_tests_before_commit": False}
    d = Dev(cfg, free_mb=lambda: 99999)
    return d, {t.name: t for t in build_tools(cfg, d)}


@pytest.fixture
def env(tmp_path, remote):
    d, tools = make(tmp_path, remote)
    out = tools["dev_start"].handler({"repo": "proj", "slug": "add-feature"})
    return d, tools, out["worktree"], tmp_path, remote


def test_levels_follow_the_policy(env):
    _, tools, *_ = env
    levels = {n: t.level for n, t in tools.items()}
    assert levels["dev_push"] is Level.R2 and levels["dev_pr_draft"] is Level.R2
    assert levels["dev_start"] is levels["dev_write"] is levels["dev_run"] is levels["dev_commit"] is Level.R1
    assert levels["dev_read"] is levels["dev_diff"] is levels["dev_files"] is Level.R0
    assert not any(n in tools for n in ("dev_force_push", "dev_delete_branch"))  # R3 は実装しない


def test_no_repos_means_no_tools():
    assert build_tools({}) == [] and build_tools({"repos": [{"name": "x"}]}) == []


def test_start_creates_feature_worktree_in_dedicated_clone(env):
    d, tools, wid, tmp_path, _ = env
    assert wid == "proj/add-feature"
    wt = tmp_path / "work" / "worktrees" / "proj" / "add-feature"
    assert (wt / "app.py").is_file() and d.branch(wt) == "feature/add-feature"
    assert (tmp_path / "work" / "clones" / "proj" / ".git").exists()
    assert tools["dev_start"].handler({"repo": "proj", "slug": "add-feature"})["branch"] == "feature/add-feature"  # 再実行しても壊れない


@pytest.mark.parametrize("slug", ["Bad Slug", "../x", "a", "x/y", "UPPER", "-lead"])
def test_start_rejects_bad_slug(env, slug):
    _, tools, *_ = env
    assert tools["dev_start"].handler({"repo": "proj", "slug": slug})["error"] == "dev_error"


def test_write_replace_and_read_back(env):
    _, tools, wid, *_ = env
    assert tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v1", "new": "v2"})["bytes"] > 0
    assert "v2" in tools["dev_read"].handler({"worktree": wid, "path": "app.py"})["text"]
    assert tools["dev_write"].handler({"worktree": wid, "path": "sub/new.py", "content": "x = 1\n"})["path"] == "sub/new.py"
    assert "sub/new.py" not in tools["dev_files"].handler({"worktree": wid})["files"]  # 未追跡


def test_write_old_must_match_exactly_once(env):
    _, tools, wid, *_ = env
    tools["dev_write"].handler({"worktree": wid, "path": "dup.py", "content": "a\na\n"})
    assert "2 箇所" in tools["dev_write"].handler({"worktree": wid, "path": "dup.py", "old": "a", "new": "b"})["reason"]
    assert "見つかりません" in tools["dev_write"].handler({"worktree": wid, "path": "dup.py", "old": "zzz", "new": "b"})["reason"]


@pytest.mark.parametrize("path", ["../escape.py", "..\\escape.py", "/etc/passwd", "C:/Windows/x.py", ".git/config", "a/.git/hooks/x",
                                  ".env", "sub/.env.local", "secrets/k.txt", "x.pem", "api_tokens.json"])
def test_write_refuses_escape_and_secrets(env, path):
    _, tools, wid, tmp_path, _ = env
    out = tools["dev_write"].handler({"worktree": wid, "path": path, "content": "x"})
    assert out["error"] == "dev_error"
    assert not (tmp_path / "escape.py").exists() and not (tmp_path / "work" / "worktrees" / "escape.py").exists()


def test_write_refuses_symlink_escape(env):
    d, tools, wid, tmp_path, _ = env
    wt = tmp_path / "work" / "worktrees" / "proj" / "add-feature"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (wt / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("シンボリックリンクを作れない環境")
    out = tools["dev_write"].handler({"worktree": wid, "path": "link/x.py", "content": "x"})
    assert out["error"] == "dev_error" and not (outside / "x.py").exists()


def test_write_size_limit(env):
    _, tools, wid, *_ = env
    assert tools["dev_write"].handler({"worktree": wid, "path": "big.txt", "content": "x" * 200_001})["error"] == "dev_error"


def test_bad_or_missing_worktree_id(env):
    _, tools, *_ = env
    for w in ("../x/y", "proj/nope-here", "proj", "a/b/c"):
        assert tools["dev_read"].handler({"worktree": w, "path": "app.py"})["error"] == "dev_error"


def test_run_build_passes_and_reports_output(env):
    _, tools, wid, *_ = env
    out = tools["dev_run"].handler({"worktree": wid, "kind": "build"})
    assert out["passed"] and "built" in out["output_tail"] and out["failures_so_far"] == 0


def test_fix_loop_stops_after_five_failures_and_tells_to_report(env):
    _, tools, wid, *_ = env
    for i in range(1, 6):
        out = tools["dev_run"].handler({"worktree": wid, "kind": "test"})
        assert out["passed"] is False and out["failures_so_far"] == i and out["attempts_left"] == 5 - i
    sixth = tools["dev_run"].handler({"worktree": wid, "kind": "test"})
    assert sixth["error"] == "attempt_limit" and "報告" in sixth["reason"]


def test_success_resets_failure_count(env):
    d, tools, wid, *_ = env
    for _ in range(3):
        tools["dev_run"].handler({"worktree": wid, "kind": "test"})
    assert d._fails[wid] == 3
    tools["dev_run"].handler({"worktree": wid, "kind": "build"})
    assert d._fails[wid] == 0


def test_run_waits_when_memory_is_low(tmp_path, remote):
    cfg = {"work_dir": str(tmp_path / "w"), "min_free_memory_mb": 1500, "repos": [{"name": "proj", "remote_url": str(remote), "build": [sys.executable, "-c", "pass"]}]}
    d = Dev(cfg, free_mb=lambda: 500)
    tools = {t.name: t for t in build_tools(cfg, d)}
    wid = tools["dev_start"].handler({"repo": "proj", "slug": "low-mem"})["worktree"]
    assert tools["dev_run"].handler({"worktree": wid, "kind": "build"})["error"] == "low_memory"


def test_run_uses_only_configured_commands_and_clean_env(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("MY_SECRET_API_KEY", "leakme")
    cfg = {"work_dir": str(tmp_path / "w"), "repos": [{"name": "proj", "remote_url": str(remote),
           "build": [sys.executable, "-c", "import os; print(os.environ.get('MY_SECRET_API_KEY', 'absent'))"]}]}
    tools = {t.name: t for t in build_tools(cfg, Dev(cfg, free_mb=lambda: 99999))}
    wid = tools["dev_start"].handler({"repo": "proj", "slug": "env-check"})["worktree"]
    assert "absent" in tools["dev_run"].handler({"worktree": wid, "kind": "build"})["output_tail"]
    assert "command" not in tools["dev_run"].input_schema["properties"]  # 任意コマンドは受け付けない


def test_diff_and_commit(env):
    _, tools, wid, tmp_path, _ = env
    tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v1", "new": "v2"})
    assert "v2" in tools["dev_diff"].handler({"worktree": wid})["diff"]
    out = tools["dev_commit"].handler({"worktree": wid, "message": "app を v2 にする"})
    assert out["committed"] and out["branch"] == "feature/add-feature" and len(out["sha"]) == 40 and out["files"] == ["app.py"]
    assert tools["dev_commit"].handler({"worktree": wid, "message": "もう一度"})["committed"] is False


def test_commit_refuses_secret_files_and_unstages(env):
    _, tools, wid, tmp_path, _ = env
    wt = tmp_path / "work" / "worktrees" / "proj" / "add-feature"
    tools["dev_write"].handler({"worktree": wid, "path": "ok.py", "content": "x=1\n"})
    (wt / ".env").write_text("TOKEN=abc\n", encoding="utf-8")  # dev_write では作れないが、ビルド生成物などで紛れ込む想定
    out = tools["dev_commit"].handler({"worktree": wid, "message": "ok を追加"})
    assert out["error"] == "dev_error" and ".env" in out["reason"]
    assert git(wt, "diff", "--cached", "--name-only").strip() == ""  # 失敗時はステージを戻す


def test_commit_and_push_refuse_non_feature_branch(env):
    d, tools, wid, tmp_path, _ = env
    wt = tmp_path / "work" / "worktrees" / "proj" / "add-feature"
    git(wt, "checkout", "-q", "-b", "main-hack")
    for name, args in (("dev_commit", {"worktree": wid, "message": "xxx"}), ("dev_push", {"worktree": wid})):
        assert tools[name].handler(args)["error"] == "dev_error"
    git(wt, "checkout", "-q", "feature/add-feature")


def test_push_goes_to_feature_branch_only_without_force(env):
    d, tools, wid, tmp_path, remote = env
    tools["dev_write"].handler({"worktree": wid, "path": "n.py", "content": "n=1\n"})
    tools["dev_commit"].handler({"worktree": wid, "message": "n を追加"})
    assert tools["dev_push"].handler({"worktree": wid}) == {"pushed": "feature/add-feature"}
    heads = git(tmp_path, "--git-dir", str(remote), "branch", "--list").split()
    assert "feature/add-feature" in heads and "main" in heads
    main_log = git(tmp_path, "--git-dir", str(remote), "log", "main", "--format=%s").split("\n")
    assert main_log[0] == "init"  # main は変わっていない
    calls = []
    real = d.run
    d.run = lambda cmd, **k: calls.append(cmd) or real(cmd, **k)
    tools["dev_push"].handler({"worktree": wid})
    assert not any("--force" in c or "-f" in c or "--delete" in c for c in calls)


def test_pr_draft_uses_gh_with_draft_flag(tmp_path, remote):
    seen = []

    def fake_run(cmd, **k):
        if cmd[0] == "gh":
            seen.append(cmd)
            return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/pull/9\n", stderr="")
        return subprocess.run(cmd, **k)

    cfg = {"work_dir": str(tmp_path / "w"), "repos": [{"name": "proj", "remote_url": str(remote), "base_ref": "origin/main"}]}
    tools = {t.name: t for t in build_tools(cfg, Dev(cfg, run=fake_run, free_mb=lambda: 99999))}
    wid = tools["dev_start"].handler({"repo": "proj", "slug": "pr-test"})["worktree"]
    out = tools["dev_pr_draft"].handler({"worktree": wid, "title": "タイトル", "body": "本文"})
    assert out["draft_pr"].endswith("/pull/9")
    cmd = seen[0]
    assert "--draft" in cmd and cmd[cmd.index("--base") + 1] == "main" and cmd[cmd.index("--head") + 1] == "feature/pr-test"


def test_r2_tools_need_approval_through_the_agent(env, audit):
    d, tools, wid, tmp_path, remote = env
    reg = ToolRegistry()
    for t in tools.values():
        reg.register(t)
    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("dev_push", {"worktree": wid})]},
                       {"role": "assistant", "content": "承認待ちです"}])
    ag = Agent(llm, reg, audit)
    ag.run("push して")
    heads = git(tmp_path, "--git-dir", str(remote), "branch", "--list").split()
    assert "feature/add-feature" not in heads                       # 承認前は push されていない
    ap = ag.approvals.list()[0]
    assert ap["tool"] == "dev_push" and "push" in ap["summary"]
    assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"]
    assert "feature/add-feature" in git(tmp_path, "--git-dir", str(remote), "branch", "--list").split()


def test_failed_r2_result_is_not_marked_executed(env, audit):
    d, tools, wid, tmp_path, _ = env
    reg = ToolRegistry()
    reg.register(tools["dev_push"])
    ag = Agent(ScriptedLLM([]), reg, audit)
    git(tmp_path / "work" / "worktrees" / "proj" / "add-feature", "checkout", "-q", "-b", "wip")
    ap = ag.approvals.request("r", "dev_push", {"worktree": wid})
    assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["status"] == "failed"


def test_helpers():
    assert is_secret_path(".env") and is_secret_path("a/secrets/x") and is_secret_path("k.pem") and not is_secret_path("src/main.py")
    with pytest.raises(Exception):
        safe_path(Path("."), "../x")



# --- 壊れた変更を先へ進めない（実機の 9B が、引用符の閉じ忘れで構文エラーのファイルを作った）---
def make_gate(tmp_path, remote, test_cmd):
    repo = {"name": "proj", "remote_url": str(remote), "base_ref": "origin/main", "test": test_cmd}
    cfg = {"work_dir": str(tmp_path / "work"), "repos": [repo]}
    d = Dev(cfg, free_mb=lambda: 99999)
    tools = {t.name: t for t in build_tools(cfg, d)}
    return tools, tools["dev_start"].handler({"repo": "proj", "slug": "gate-test"})["worktree"], tmp_path / "work" / "worktrees" / "proj" / "gate-test"


@pytest.mark.parametrize("path,bad,good", [
    ("app.py", 'WEEKDAYS = "月火水\n', "X = 1\n"),
    ("data.json", "{'a': 1,}", '{"a": 1}'),
    ("conf.toml", "a = = 1", "a = 1\n"),
])
def test_broken_python_json_toml_are_not_written(tmp_path, remote, path, bad, good):
    tools, wid, wt = make_gate(tmp_path, remote, [sys.executable, "-c", "pass"])
    before = (wt / path).read_text(encoding="utf-8") if (wt / path).exists() else None
    out = tools["dev_write"].handler({"worktree": wid, "path": path, "content": bad})
    assert out["error"] == "dev_error" and ("構文エラー" in out["reason"] or "形式が不正" in out["reason"])
    assert ((wt / path).read_text(encoding="utf-8") if (wt / path).exists() else None) == before     # ファイルは変わらない
    assert "bytes" in tools["dev_write"].handler({"worktree": wid, "path": path, "content": good})   # 正しい内容なら書ける


def test_syntax_error_is_reported_with_line_number_for_str_replace(tmp_path, remote):
    tools, wid, wt = make_gate(tmp_path, remote, [sys.executable, "-c", "pass"])
    out = tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "print('v1')", "new": "print('v1"})   # 引用符を消す
    assert out["error"] == "dev_error" and "行目" in out["reason"] and (wt / "app.py").read_text() == "print('v1')\n"


def test_non_code_files_are_not_checked(tmp_path, remote):
    tools, wid, _ = make_gate(tmp_path, remote, [sys.executable, "-c", "pass"])
    assert "bytes" in tools["dev_write"].handler({"worktree": wid, "path": "notes.md", "content": "「閉じない\n"})


def test_commit_requires_a_passing_test_after_the_last_change(tmp_path, remote):
    tools, wid, wt = make_gate(tmp_path, remote, [sys.executable, "-c", "pass"])
    tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v1", "new": "v2"})
    out = tools["dev_commit"].handler({"worktree": wid, "message": "v2 にする"})
    assert out["error"] == "dev_error" and "テストが通っていません" in out["reason"]
    assert git(wt, "log", "--oneline").count("\n") == 1                                   # 初期コミットのみ（新しいコミットはできていない）
    assert tools["dev_run"].handler({"worktree": wid, "kind": "test"})["passed"]
    assert tools["dev_commit"].handler({"worktree": wid, "message": "v2 にする"})["committed"] is True
    tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v2", "new": "v3"})   # また変更したら、もう一度テストが要る
    assert tools["dev_commit"].handler({"worktree": wid, "message": "v3"})["error"] == "dev_error"


def test_failing_tests_do_not_unlock_the_commit(tmp_path, remote):
    tools, wid, _ = make_gate(tmp_path, remote, [sys.executable, "-c", "import sys; sys.exit(1)"])
    tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v1", "new": "v2"})
    assert tools["dev_run"].handler({"worktree": wid, "kind": "test"})["passed"] is False
    assert tools["dev_commit"].handler({"worktree": wid, "message": "壊れたまま"})["error"] == "dev_error"


def test_build_does_not_unlock_the_commit_and_repos_without_tests_are_not_gated(tmp_path, remote):
    tools, wid, _ = make_gate(tmp_path, remote, [sys.executable, "-c", "pass"])
    tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v1", "new": "v2"})
    assert tools["dev_commit"].handler({"worktree": wid, "message": "x"})["error"] == "dev_error"
    tools2, wid2, _ = make_gate(tmp_path / "second", remote, [])                          # テストが設定されていないリポジトリは、従来どおり
    tools2["dev_write"].handler({"worktree": wid2, "path": "app.py", "old": "v1", "new": "v2"})
    assert tools2["dev_commit"].handler({"worktree": wid2, "message": "x"})["committed"] is True
