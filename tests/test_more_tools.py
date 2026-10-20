"""current_time / repo_read / git_info / audit_search のテスト。"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.app import build_registry
from core.audit import AuditLog
from policy.levels import Level
from tools import audit_search, clock, repo


def git(r: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@e.com", *a], check=True, capture_output=True)


@pytest.fixture
def cfg(tmp_path: Path) -> dict:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 301)), encoding="utf-8")
    (r / ".env").write_text("TOKEN=zzz\n", encoding="utf-8")
    (r / "secrets").mkdir()
    (r / "secrets" / "k.txt").write_text("leak\n", encoding="utf-8")
    (r / "b.bin.md").write_bytes(b"\0binary")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first commit")
    (r / "a.py").write_text("changed\n", encoding="utf-8")
    git(r, "commit", "-qam", "second commit")
    return {"repos": [{"name": "r", "path": str(r)}]}


def test_clock_is_jst_and_r0():
    t = clock.build_tool(now=lambda: datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc))
    out = t.handler({})
    assert t.level is Level.R0 and out["date"] == "2026-10-21" and out["time"] == "00:30" and out["weekday"] == "水曜日"


def test_repo_read_line_range_with_numbers_and_sha(cfg):
    t = repo.build_read_tool(cfg)
    old = subprocess.run(["git", "-C", cfg["repos"][0]["path"], "rev-parse", "HEAD~1"], capture_output=True, text=True).stdout.strip()
    out = t.handler({"repo": "r", "path": "a.py"})
    assert out["text"] == "1: changed" and len(out["commit"]) == 40 and out["total_lines"] == 1
    assert old != out["commit"]


def test_repo_read_caps_at_200_lines(tmp_path):
    r = tmp_path / "big"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 301)), encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "x")
    out = repo.build_read_tool({"repos": [{"name": "r", "path": str(r)}]}).handler({"repo": "r", "path": "a.py", "start": 10, "end": 290})
    assert out["lines"] == "10-209" and out["text"].splitlines()[0] == "10: line10"


@pytest.mark.parametrize("path", [".env", "secrets/k.txt", "../x.py", "..\\x.py", "/etc/passwd"])
def test_repo_read_refuses_secrets_and_traversal(cfg, path):
    out = repo.build_read_tool(cfg).handler({"repo": "r", "path": path})
    assert out["error"] in ("blocked", "not_found") and "leak" not in str(out) and "zzz" not in str(out)


def test_repo_read_missing_binary_and_range(cfg):
    t = repo.build_read_tool(cfg)
    assert t.handler({"repo": "r", "path": "nope.py"})["error"] == "not_found"
    assert t.handler({"repo": "r", "path": "b.bin.md"})["error"] == "blocked"
    assert t.handler({"repo": "r", "path": "a.py", "start": 50})["error"] == "out_of_range"


def test_git_log_show_branches(cfg):
    t = repo.build_git_tool(cfg)
    log = t.handler({"repo": "r", "action": "log"})["commits"]
    assert [c["subject"] for c in log] == ["second commit", "first commit"] and len(log[0]["sha"]) == 40
    assert "a.py" in t.handler({"repo": "r", "action": "show", "sha": log[0]["sha"]})["text"]
    assert any("main" in b or "master" in b for b in t.handler({"repo": "r", "action": "branches"})["branches"])
    assert len(t.handler({"repo": "r", "action": "log", "path": "a.py", "limit": 1})["commits"]) == 1


@pytest.mark.parametrize("sha", ["--output=x", "HEAD; rm -rf", "zz", ""])
def test_git_show_rejects_non_sha(cfg, sha):
    assert repo.build_git_tool(cfg).handler({"repo": "r", "action": "show", "sha": sha})["error"] == "invalid_sha"


def test_git_log_refuses_secret_path(cfg):
    assert repo.build_git_tool(cfg).handler({"repo": "r", "action": "log", "path": ".env"})["error"] == "blocked"


def test_audit_search_tool(tmp_path):
    a = AuditLog(tmp_path / "audit")
    a.write("tool_call", "r1", tool="network", status="ok")
    a.write("request", "r1", request="pingして")
    t = audit_search.build_tool(a)
    assert t.level is Level.R0
    assert [e["tool"] for e in t.handler({"tool": "network"})["events"]] == ["network"]


def test_registry_includes_new_tools_all_with_levels(cfg, tmp_path):
    full = {"llm": {"endpoints": []}, "search": cfg, "network": {}, "web": {}, "host_status": {}}
    full["search"] = {"repos": cfg["repos"], "index_path": str(tmp_path / "i.sqlite3")}
    reg = build_registry(full, AuditLog(tmp_path / "audit"))
    assert {"current_time", "repo_read", "git_info", "audit_search", "search", "web_fetch", "host_status", "network"} <= set(reg.names())
    assert all(reg.get(n).level in (Level.R0, Level.R1) for n in reg.names())
