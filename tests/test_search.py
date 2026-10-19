"""search のテスト。一時ディレクトリに本物の git リポジトリを作って検証する（ネットワーク・実機は不要）。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from policy.levels import Level
from tools.search import Index, build_tool, chunk_text, is_indexable

PY = '''import os

def alpha_handler(x):
    """alpha を処理する"""
    return x + 1


class BetaWorker:
    def run(self):
        return "beta_marker_value"
'''


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.py").write_text(PY, encoding="utf-8")
    (r / "notes.md").write_text("# 設計メモ\nレンダラーの初期化順序について\n\n## 物理\nbullet_world を作る\n", encoding="utf-8")
    (r / ".env").write_text("SECRET_TOKEN=abc123xyz\n", encoding="utf-8")
    (r / "secrets").mkdir()
    (r / "secrets" / "k.txt").write_text("leaky_secret_value\n", encoding="utf-8")
    (r / "bin.dat").write_bytes(b"\0\1\2leaky_binary")
    (r / "build").mkdir()
    (r / "build" / "gen.py").write_text("def generated_noise(): pass\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "初期コミット: alpha_handler を追加")
    return r


def test_python_chunks_by_function_and_class():
    ch = chunk_text("a.py", PY)
    names = [c[3] for c in ch]
    assert "alpha_handler" in names and "BetaWorker" in names
    beta = next(c for c in ch if c[3] == "BetaWorker")
    assert "beta_marker_value" in beta[4] and beta[0] == 8


def test_markdown_chunks_by_heading():
    names = [c[3] for c in chunk_text("n.md", "# A\nx\n## B\ny\n")]
    assert names == ["A", "B"]


def test_c_family_chunks_by_function():
    src = "#include <x>\n\nint Add(int a, int b) {\n  return a + b;\n}\n\nclass Foo {\n};\n"
    names = [c[3] for c in chunk_text("x.cpp", src)]
    assert any("Add" in n for n in names) and any("class Foo" in n for n in names)


def test_long_chunk_is_split():
    ch = chunk_text("big.txt", "\n".join(f"line{i}" for i in range(300)))
    assert len(ch) == 3 and ch[1][0] == 121


@pytest.mark.parametrize("path,ok", [("src/a.py", True), (".env", False), (".env.local", False), ("secrets/x.txt", False),
                                     ("build/o.py", False), ("a/node_modules/x.js", False), ("key.pem", False),
                                     ("img.png", False), ("api_tokens.json", False), ("docs/readme.md", True)])
def test_is_indexable(path, ok):
    assert is_indexable(path, 100, 1000) is ok


def test_oversize_not_indexable():
    assert is_indexable("a.py", 5000, 1000) is False


def test_search_finds_function_with_evidence(repo: Path):
    idx = Index(":memory:")
    idx.update_repo("r", repo)
    hits = idx.query("alpha_handler", "r")
    top = hits[0]
    assert top["path"] == "a.py" and top["symbol"] == "alpha_handler"
    assert top["lines"].startswith("3-") and len(top["blob_sha"]) == 40 and len(top["indexed_commit"]) == 40


def test_search_japanese_and_commit_history(repo: Path):
    idx = Index(":memory:")
    idx.update_repo("r", repo)
    assert idx.query("レンダラー")[0]["path"] == "notes.md"
    c = [h for h in idx.query("初期コミット") if h["kind"] == "commit"][0]
    assert len(c["commit"]) == 40


def test_excluded_content_never_indexed(repo: Path):
    idx = Index(":memory:")
    idx.update_repo("r", repo)
    for word in ("abc123xyz", "SECRET_TOKEN", "leaky_secret_value", "leaky_binary", "generated_noise"):
        assert idx.query(word) == [], word


def test_incremental_update_only_touches_changed_files(repo: Path):
    idx = Index(":memory:")
    first = idx.update_repo("r", repo)
    assert first["added"] == 2 and first["unchanged"] == 0
    same = idx.update_repo("r", repo)
    assert same == {"added": 0, "updated": 0, "removed": 0, "unchanged": 2}
    (repo / "a.py").write_text(PY.replace("beta_marker_value", "gamma_marker_value"), encoding="utf-8")
    (repo / "new.md").write_text("# 新規\nzeta_topic\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "更新")
    r = idx.update_repo("r", repo)
    assert r == {"added": 1, "updated": 1, "removed": 0, "unchanged": 1}
    assert idx.query("beta_marker_value") == [] and idx.query("gamma_marker_value")
    git(repo, "rm", "-q", "new.md")
    git(repo, "commit", "-q", "-m", "削除")
    assert idx.update_repo("r", repo)["removed"] == 1 and idx.query("zeta_topic") == []


def test_short_or_empty_query_returns_empty(repo: Path):
    idx = Index(":memory:")
    idx.update_repo("r", repo)
    assert idx.query("ab") == [] and idx.query("   ") == []


def test_query_with_quotes_does_not_break(repo: Path):
    idx = Index(":memory:")
    idx.update_repo("r", repo)
    hits = idx.query('"alpha_handler" OR NEAR(')  # FTS の構文として解釈されず、例外にもならない
    assert hits and hits[0]["symbol"] == "alpha_handler"


def test_tool_level_schema_and_handler(repo: Path):
    t = build_tool({"repos": [{"name": "r", "path": str(repo)}]}, index=Index(":memory:"))
    assert t.level is Level.R0 and t.input_schema["properties"]["repo"]["enum"] == ["r"]
    out = t.handler({"query": "BetaWorker"})
    assert out["hits"][0]["path"] == "a.py" and out["index_update"]["r"]["added"] == 2


def test_tool_without_repos_reports_error():
    out = build_tool({}, index=Index(":memory:")).handler({"query": "anything"})
    assert out["error"] == "no_repos"


def test_tool_reports_git_failure_without_raising(tmp_path: Path):
    t = build_tool({"repos": [{"name": "x", "path": str(tmp_path / "nope")}]}, index=Index(":memory:"))
    out = t.handler({"query": "something"})
    assert "error" in out["index_update"]["x"] and out["hits"] == []
