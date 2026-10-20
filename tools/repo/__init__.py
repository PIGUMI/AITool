"""repo_read / git_info（R0）：登録リポジトリの読み取り専用の参照。

search で見つけた箇所を、行番号つきで実際に読んで根拠を確かめるために使う。
git の読み取りコマンドだけを使い、作業ツリーには触れない。除外対象（.env・secrets 等）は読まない。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from policy.levels import Level
from policy.registry import ToolSpec
from tools.search import _git, is_indexable

SHA = re.compile(r"^[0-9a-fA-F]{4,40}$")
MAX_LINES = 200


def _repos(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in cfg.get("repos", []) if r.get("name") and r.get("path")}


def build_read_tool(cfg: dict[str, Any]) -> ToolSpec:
    """repo_read: 登録リポジトリの tracked ファイルを、行範囲を指定して読む。"""
    repos = _repos(cfg)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "enum": sorted(repos)},
            "path": {"type": "string", "description": "リポジトリ内の相対パス（search の path）"},
            "start": {"type": "integer", "minimum": 1, "description": "開始行（既定 1）"},
            "end": {"type": "integer", "minimum": 1, "description": f"終了行（開始から最大 {MAX_LINES} 行）"},
        },
        "required": ["repo", "path"],
        "additionalProperties": False,
    }

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        r = repos[args["repo"]]
        path = args["path"].replace("\\", "/").lstrip("/")
        if ".." in Path(path).parts or not is_indexable(path, 0, 1):
            return {"error": "blocked", "reason": "読めない種類・場所のファイルです（認証情報・生成物・バイナリ等は除外）"}
        ref = r.get("ref", "HEAD")
        try:
            commit = _git(Path(r["path"]), "rev-parse", ref).decode().strip()
            data = _git(Path(r["path"]), "show", f"{commit}:{path}")
        except RuntimeError:
            return {"error": "not_found", "reason": f"{path} は {ref} にありません"}
        if b"\0" in data[:4096]:
            return {"error": "blocked", "reason": "バイナリです"}
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = int(args.get("start", 1))
        end = min(int(args.get("end", start + MAX_LINES - 1)), start + MAX_LINES - 1, len(lines))
        if start > len(lines):
            return {"error": "out_of_range", "reason": f"{path} は {len(lines)} 行です"}
        return {"repo": args["repo"], "path": path, "commit": commit, "total_lines": len(lines),
                "lines": f"{start}-{end}", "text": "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))}

    return ToolSpec("repo_read", "登録リポジトリのファイルを行番号つきで読む（最大200行。コミットSHAを返す）。", schema, Level.R0, handler)


def build_git_tool(cfg: dict[str, Any]) -> ToolSpec:
    """git_info: 履歴・コミットの詳細・ブランチ一覧（読み取り専用）。"""
    repos = _repos(cfg)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "enum": sorted(repos)},
            "action": {"type": "string", "enum": ["log", "show", "branches"]},
            "sha": {"type": "string", "description": "show で見るコミットSHA（4〜40桁）"},
            "path": {"type": "string", "description": "log を特定ファイルに絞る場合の相対パス"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "required": ["repo", "action"],
        "additionalProperties": False,
    }

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        r = repos[args["repo"]]
        repo, ref = Path(r["path"]), r.get("ref", "HEAD")
        act = args["action"]
        try:
            if act == "branches":
                out = _git(repo, "branch", "-a", "--format=%(refname:short) %(objectname:short)").decode("utf-8", "replace")
                return {"branches": out.splitlines()[:100]}
            if act == "show":
                if not SHA.match(args.get("sha", "")):
                    return {"error": "invalid_sha", "reason": "sha は 4〜40桁の16進数で指定してください"}
                out = _git(repo, "show", "--stat", "--format=%H%n%an %ad%n%B", "--date=short", args["sha"], "--").decode("utf-8", "replace")
                return {"text": out[:4000]}
            cmd = ["log", f"-{int(args.get('limit', 10))}", "--format=%H|%ad|%an|%s", "--date=short", ref]
            if args.get("path"):
                path = args["path"].replace("\\", "/").lstrip("/")
                if ".." in Path(path).parts or not is_indexable(path, 0, 1):
                    return {"error": "blocked", "reason": "対象外のパスです"}
                cmd += ["--", path]
            rows = [x.split("|", 3) for x in _git(repo, *cmd).decode("utf-8", "replace").splitlines()]
            return {"commits": [{"sha": a, "date": b, "author": c, "subject": d} for a, b, c, d in rows]}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return {"error": "git_failed", "reason": str(e)[:200]}

    return ToolSpec("git_info", "登録リポジトリのコミット履歴（log）・コミット詳細（show）・ブランチ一覧（branches）を読み取る。", schema, Level.R0, handler)
