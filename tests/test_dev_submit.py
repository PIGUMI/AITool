"""dev_submit（push と PR 作成を1回の承認で）のテスト。ローカルの bare リポジトリと、GitHub API のモックを使う。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.agent import Agent
from policy.levels import Level
from policy.registry import ToolRegistry
from tests.conftest import ScriptedLLM, tool_call
from tools.dev import DevError, Dev, _http, build_tools, parse_github

ID = ["-c", "user.name=t", "-c", "user.email=t@e.com"]
FAKE_TOKEN = "github_pat_" + "A" * 30


def git(cwd, *a):
    return subprocess.run(["git", *ID, *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class FakeGitHub:
    """GitHub API のモック。呼び出しを記録し、決めた応答を返す。"""

    def __init__(self):
        self.calls, self.post_status, self.post_body, self.existing = [], 201, {"html_url": "https://github.com/o/r/pull/9", "number": 9}, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), json.loads(body) if body else None))
        if method == "POST":
            return self.post_status, self.post_body
        return 200, self.existing


@pytest.fixture
def env(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setenv("ASSISTANT_SECRETS_DIR", str(secrets))
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "app.py").write_text("print('v1')\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "main")
    gh = FakeGitHub()
    cfg = {"work_dir": str(tmp_path / "work"), "repos": [{"name": "proj", "remote_url": str(bare), "base_ref": "origin/main", "github_repo": "o/r",
                                                          "test": [sys.executable, "-c", "pass"]}]}
    cfg["require_tests_before_commit"] = False   # このテストは push と PR が対象。コミットの条件は test_dev.py で確認
    d = Dev(cfg, free_mb=lambda: 99999, http=gh)
    tools = {t.name: t for t in build_tools(cfg, d)}
    wid = tools["dev_start"].handler({"repo": "proj", "slug": "feat-x"})["worktree"]
    tools["dev_write"].handler({"worktree": wid, "path": "app.py", "old": "v1", "new": "v2"})
    tools["dev_commit"].handler({"worktree": wid, "message": "app を v2 に"})
    return {"d": d, "tools": tools, "wid": wid, "gh": gh, "bare": bare, "secrets": secrets, "tmp": tmp_path, "cfg": cfg}


def with_token(env):
    (env["secrets"] / "github_token.txt").write_text(FAKE_TOKEN, encoding="utf-8")


def remote_branches(env):
    return git(env["tmp"], "--git-dir", str(env["bare"]), "branch", "--list").split()


# --- 設計 ---
def test_level_flags_and_schema(env):
    t = env["tools"]["dev_submit"]
    assert t.level is Level.R2 and t.egress and t.bind and t.summarizer
    assert set(t.input_schema["required"]) == {"worktree", "title", "body"} and "expect_head" not in t.input_schema["properties"]
    assert env["tools"]["dev_push"].egress and env["tools"]["dev_pr_draft"].egress


@pytest.mark.parametrize("url,expected", [("https://github.com/PIGUMI/Faraway.git", ("PIGUMI", "Faraway")), ("https://github.com/o/r", ("o", "r")),
                                          ("git@github.com:o/r.git", ("o", "r")), ("ssh://git@github.com/o/r.git", ("o", "r")),
                                          ("https://evil.example/o/r.git", None), ("C:\\repo\\x.git", None), ("https://github.com.evil.com/o/r", None)])
def test_parse_github(url, expected):
    assert parse_github(url) == expected


# --- 正常系 ---
def test_submit_pushes_and_creates_draft_pr(env):
    with_token(env)
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "app を v2 に", "body": "テストが通りました"})
    assert out["pushed"] == "feature/feat-x" and out["pull_request"] == "https://github.com/o/r/pull/9" and out["number"] == 9
    assert "feature/feat-x" in remote_branches(env)
    method, url, headers, payload = env["gh"].calls[0]
    assert method == "POST" and url == "https://api.github.com/repos/o/r/pulls"
    assert payload["head"] == "feature/feat-x" and payload["base"] == "main" and payload["draft"] is True      # 既定は下書き。main 宛て
    assert payload["title"] == "app を v2 に" and "Faraway が作成した PR" in payload["body"] and "テストが通りました" in payload["body"]
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_main_is_never_touched(env):
    with_token(env)
    env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert git(env["tmp"], "--git-dir", str(env["bare"]), "log", "main", "--format=%s").split("\n")[0] == "init"


def test_pr_draft_flag_is_configurable(env):
    with_token(env)
    env["d"].cfg["pr_draft"] = False
    env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert env["gh"].calls[0][3]["draft"] is False


# --- トークンが無い・API が失敗したとき ---
def test_without_token_pushes_and_returns_compare_url(env):
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert out["pushed"] == "feature/feat-x" and out["pull_request"] is None and env["gh"].calls == []
    assert out["compare_url"] == "https://github.com/o/r/compare/main...feature/feat-x?expand=1" and "トークンが未設定" in out["note"]


def test_api_failure_keeps_the_push_and_gives_a_link(env):
    with_token(env)
    env["gh"].post_status, env["gh"].post_body = 403, {"message": "Resource not accessible by personal access token"}
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert "feature/feat-x" in remote_branches(env) and out["pull_request"] is None and "HTTP 403" in out["note"] and out["compare_url"]
    assert FAKE_TOKEN not in json.dumps(out)                                    # トークンは結果に出ない


def test_existing_pr_is_returned(env):
    with_token(env)
    env["gh"].post_status, env["gh"].post_body = 422, {"message": "A pull request already exists"}
    env["gh"].existing = [{"html_url": "https://github.com/o/r/pull/3", "number": 3}]
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert out["pull_request"] == "https://github.com/o/r/pull/3" and "すでに PR" in out["note"]
    assert "head=o:feature/feat-x" in env["gh"].calls[1][1]


def test_non_github_remote_has_no_pr(env):
    env["d"].repos["proj"]["github_repo"] = ""
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert out["pull_request"] is None and "GitHub のリポジトリではない" in out["note"] and env["gh"].calls == []


# --- 拒否 ---
@pytest.mark.parametrize("body", ["token=abc123", "Bearer abcdef", "password: hunter2", "x" * 45, FAKE_TOKEN, "ghp_" + "B" * 30])
def test_secret_like_text_is_never_sent(env, body):
    with_token(env)
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "題名です", "body": body})
    assert out["error"] == "dev_error" and "秘匿" in out["reason"] and env["gh"].calls == [] and "feature/feat-x" not in remote_branches(env)


def test_refuses_non_feature_branch(env):
    git(env["d"].wt(env["wid"]), "checkout", "-q", "-b", "wip")
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "t t t", "body": "b b b"})
    assert out["error"] == "dev_error" and env["gh"].calls == []


def test_http_helper_only_talks_to_api_github():
    with pytest.raises(DevError, match="api.github.com"):
        _http("POST", "https://evil.example/x", {}, b"{}", 5)
    with pytest.raises(DevError, match="api.github.com"):
        _http("GET", "https://api.github.com.evil.com/x", {}, None, 5)


# --- 承認との結合: 差分の要約・HEAD の束縛・1回限り ---
def agent_for(env, audit, tool_args):
    reg = ToolRegistry()
    for t in env["tools"].values():
        reg.register(t)
    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("dev_submit", tool_args)]}, {"role": "assistant", "content": "承認待ちです"}])
    return Agent(llm, reg, audit)


def test_approval_shows_diff_summary_and_binds_head(env, audit):
    with_token(env)
    ag = agent_for(env, audit, {"worktree": env["wid"], "title": "app を v2 に", "body": "説明です"})
    ag.run("PR を出して")
    assert env["gh"].calls == [] and "feature/feat-x" not in remote_branches(env)                 # 承認前は何も出ていかない
    ap = ag.approvals.list()[0]
    head = env["d"].head(env["wid"])
    assert ap["args"]["expect_head"] == head                                                       # 承認時点のコミットが引数（＝ハッシュ）に束ねられる
    assert "app.py" in ap["summary"] and "下書き" in ap["summary"] and "マージは GitHub で人が行います" in ap["summary"] and head[:12] in ap["summary"]
    assert "app を v2 に" in ap["summary"] and "説明です" in ap["summary"]
    assert ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"]
    assert "feature/feat-x" in remote_branches(env) and len(env["gh"].calls) == 1                  # 1回の承認で push と PR
    assert not ag.approvals.approve(ap["id"], ap["hash"], "phone")["ok"] and len(env["gh"].calls) == 1


def test_changes_after_the_request_are_not_pushed(env, audit):
    with_token(env)
    ag = agent_for(env, audit, {"worktree": env["wid"], "title": "app を v2 に", "body": "説明です"})
    ag.run("PR を出して")
    ap = ag.approvals.list()[0]
    env["tools"]["dev_write"].handler({"worktree": env["wid"], "path": "app.py", "old": "v2", "new": "v3-unreviewed"})   # 承認の前に、中身が変わった
    env["tools"]["dev_commit"].handler({"worktree": env["wid"], "message": "承認後の追加コミット"})
    out = ag.approvals.approve(ap["id"], ap["hash"], "phone")
    assert out["status"] == "failed" and "承認のあとに" in json.dumps(out["result"], ensure_ascii=False)
    assert "feature/feat-x" not in remote_branches(env) and env["gh"].calls == []                  # 承認していない中身は、出ていかない


def test_summary_failure_does_not_block_the_request(env, audit, monkeypatch):
    ag = agent_for(env, audit, {"worktree": env["wid"], "title": "app を v2 に", "body": "説明です"})
    monkeypatch.setattr(env["d"], "submit_preview", lambda a: (_ for _ in ()).throw(RuntimeError("x")))
    ag.run("PR を出して")
    assert "dev_submit" == ag.approvals.list()[0]["tool"] and "PR" in ag.approvals.list()[0]["summary"]


def test_token_is_masked_in_the_audit_log(env, audit):
    audit.add_secret(FAKE_TOKEN)
    ag = agent_for(env, audit, {"worktree": env["wid"], "title": "t t t", "body": f"本文 {FAKE_TOKEN}"})
    ag.run("PR を出して")
    assert FAKE_TOKEN not in json.dumps(audit.read(), ensure_ascii=False)


def test_commit_shas_in_the_body_are_allowed(env):
    with_token(env)
    sha = env["d"].head(env["wid"])
    out = env["tools"]["dev_submit"].handler({"worktree": env["wid"], "title": "SHA を含む", "body": f"{sha} の続きです（コミット {sha}）"})
    assert out["pull_request"] and not out.get("error")
