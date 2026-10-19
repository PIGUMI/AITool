"""dev（R0〜R2）：feature ブランチ・git worktree での開発作業。

- 作業は専用の clone（work_dir/clones/<repo>）とその worktree（work_dir/worktrees/<repo>/<slug>）の中だけ。
  常駐PCのメインのチェックアウトには触れない。ブランチは必ず feature/<slug>。
- ファイル編集は worktree の中に限る（.git・秘匿ファイルは不可）。実行できるのは config に書いた build/test コマンドだけ。
- ビルド・テストの失敗は worktree ごとに数え、上限（既定5回）に達したら止まって報告する。
- push（R2）・PR下書き（R2）は承認が必要。main への push・force push・ブランチ削除は実装しない。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from policy.levels import Level
from policy.registry import ToolSpec

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
WT_ID = re.compile(r"^[A-Za-z0-9_.-]+/[a-z0-9][a-z0-9-]{1,40}$")
PROTECTED = {"main", "master", "develop", "release"}
SECRET_NAMES = {"api_tokens.json", "id_rsa", "id_ed25519", "google_token.json", "google_client.json"}
SECRET_SUFFIX = (".pem", ".key", ".pfx", ".p12")
MAX_WRITE = 200_000
OUT_TAIL = 3000
ENV_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
            "PROGRAMFILES", "COMSPEC", "LANG", "PYTHONIOENCODING")
Runner = Callable[..., subprocess.CompletedProcess]


class DevError(Exception):
    """利用者に見せてよいエラー（値や秘匿情報を含めない）。"""


def is_secret_path(rel: str) -> bool:
    p = Path(rel)
    low = [x.lower() for x in p.parts]
    return (".git" in low or "secrets" in low or p.name.lower().startswith(".env") or p.name.lower() in SECRET_NAMES
            or p.name.lower().endswith(SECRET_SUFFIX))


def safe_path(root: Path, rel: str) -> Path:
    """root 内の相対パスだけを許す。絶対パス・..・シンボリックリンクでの脱出・.git・秘匿ファイルは不可。"""
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in Path(rel).parts:
        raise DevError("worktree 内の相対パスを指定してください")
    if is_secret_path(rel):
        raise DevError("このパス（.git・秘匿ファイル）は扱えません")
    full = (root / rel).resolve()
    if not full.is_relative_to(root.resolve()):
        raise DevError("worktree の外には出られません")
    return full


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
    return env


def _run(cmd: list[str], cwd: Path, timeout: float, run: Runner = subprocess.run) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)  # 常駐PCの他の処理（llama-server）を妨げない
    return run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
               env=_env(), creationflags=flags)


class Dev:
    """clone・worktree・編集・ビルド/テスト・コミット・push・PR の実体。"""

    def __init__(self, cfg: dict[str, Any], run: Runner = subprocess.run, free_mb: Callable[[], int | None] | None = None) -> None:
        self.cfg, self.run = cfg, run
        self.repos = {r["name"]: r for r in cfg.get("repos", []) if r.get("name") and r.get("remote_url")}
        self.work = Path(cfg.get("work_dir") or (Path.home() / ".assistant" / "work"))
        self.max_fail = int(cfg.get("max_fix_attempts", 5))
        self.timeout = float(cfg.get("command_timeout_seconds", 600))
        self.min_free = int(cfg.get("min_free_memory_mb", 1500))
        self.free_mb = free_mb or self._free_mb
        self._fails: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _free_mb() -> int | None:
        try:
            from tools.host_status import _memory_windows
            return int(_memory_windows()["memory_available_bytes"] / 1024 / 1024)
        except Exception:
            return None  # 測れないときは止めない

    def git(self, cwd: Path, *args: str, timeout: float = 120) -> str:
        r = _run(["git", *args], cwd, timeout, self.run)
        if r.returncode != 0:
            raise DevError(f"git {args[0]} が失敗しました: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def wt(self, wid: str) -> Path:
        if not WT_ID.match(wid):
            raise DevError("worktree は 'repo/slug' の形で指定してください")
        p = self.work / "worktrees" / wid
        if not p.is_dir():
            raise DevError(f"worktree がありません: {wid}（先に dev_start を実行してください）")
        return p

    def branch(self, wt: Path) -> str:
        return self.git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()

    # --- R1 ---
    def start(self, repo: str, slug: str) -> dict[str, Any]:
        if repo not in self.repos:
            raise DevError("登録されていないリポジトリです")
        if not SLUG.match(slug):
            raise DevError("slug は小文字英数字とハイフン（2〜41文字）で指定してください")
        r = self.repos[repo]
        clone = self.work / "clones" / repo
        if not (clone / ".git").exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            self.git(clone.parent, "clone", "--quiet", r["remote_url"], str(clone), timeout=600)
        self.git(clone, "fetch", "--quiet", "origin", timeout=300)
        path = self.work / "worktrees" / repo / slug
        branch = f"feature/{slug}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git(clone, "worktree", "add", "-b", branch, str(path), r.get("base_ref", "origin/main"))
        return {"worktree": f"{repo}/{slug}", "branch": branch, "path": str(path), "base": r.get("base_ref", "origin/main")}

    def write(self, wid: str, rel: str, content: str | None, old: str | None, new: str | None) -> dict[str, Any]:
        root = self.wt(wid)
        full = safe_path(root, rel)
        if content is not None:
            if len(content) > MAX_WRITE:
                raise DevError(f"1回に書けるのは {MAX_WRITE} 文字までです")
            text = content
        else:
            if old is None or new is None or not full.is_file():
                raise DevError("content か、既存ファイルに対する old/new を指定してください")
            cur = full.read_text(encoding="utf-8")
            if cur.count(old) != 1:
                raise DevError(f"old が {cur.count(old)} 箇所に一致しました（ちょうど1箇所になるよう範囲を調整してください）")
            text = cur.replace(old, new)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8", newline="")
        return {"path": rel, "bytes": len(text.encode("utf-8"))}

    def run_cmd(self, wid: str, kind: str) -> dict[str, Any]:
        root = self.wt(wid)
        repo = wid.split("/")[0]
        cmd = self.repos.get(repo, {}).get(kind) or []
        if not cmd:
            raise DevError(f"{repo} の {kind} コマンドが config に設定されていません")
        with self._lock:
            fails = self._fails.get(wid, 0)
        if fails >= self.max_fail:
            return {"error": "attempt_limit", "failures": fails,
                    "reason": f"ビルド/テストの失敗が上限（{self.max_fail}回）に達しました。修正を続けず、ここまでの経過と詰まった箇所をユーザーに報告して停止してください。"}
        free = self.free_mb()
        if free is not None and free < self.min_free:
            return {"error": "low_memory", "free_mb": free, "reason": f"空きメモリが下限（{self.min_free}MB）を下回っているため開始しません。しばらく待ってください。"}
        try:
            r = _run(list(cmd), root, self.timeout, self.run)
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            code, out = -1, f"タイムアウト（{int(self.timeout)}秒）"
        except OSError as e:
            code, out = -1, f"起動できませんでした: {type(e).__name__}"
        with self._lock:
            self._fails[wid] = 0 if code == 0 else fails + 1
            n = self._fails[wid]
        return {"kind": kind, "exit_code": code, "passed": code == 0, "failures_so_far": n, "attempts_left": max(0, self.max_fail - n),
                "output_tail": out[-OUT_TAIL:]}

    def commit(self, wid: str, message: str) -> dict[str, Any]:
        root = self.wt(wid)
        branch = self.branch(root)
        if not branch.startswith("feature/") or branch in PROTECTED:
            raise DevError("feature/* ブランチ以外ではコミットしません")
        self.git(root, "add", "-A")
        staged = [x for x in self.git(root, "diff", "--cached", "--name-only").splitlines() if x]
        bad = [x for x in staged if is_secret_path(x)]
        if bad:
            self.git(root, "reset", "--quiet")
            raise DevError("秘匿ファイルが含まれるためコミットしません: " + ", ".join(bad[:5]))
        if not staged:
            return {"committed": False, "reason": "変更がありません"}
        who = ["-c", f"user.name={self.cfg.get('git_name', 'Assistant')}", "-c", f"user.email={self.cfg.get('git_email', 'assistant@localhost')}"]
        self.git(root, *who, "commit", "--quiet", "-m", message)
        return {"committed": True, "sha": self.git(root, "rev-parse", "HEAD").strip(), "files": staged[:30], "branch": branch}

    # --- R0 ---
    def diff(self, wid: str) -> dict[str, Any]:
        root = self.wt(wid)
        return {"branch": self.branch(root), "status": self.git(root, "status", "--short")[:2000],
                "stat": self.git(root, "diff", "HEAD", "--stat")[:2000], "diff": self.git(root, "diff", "HEAD")[:6000]}

    def read(self, wid: str, rel: str, start: int, end: int) -> dict[str, Any]:
        full = safe_path(self.wt(wid), rel)
        if not full.is_file():
            raise DevError("ファイルがありません")
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        end = min(end, start + 199, len(lines))
        return {"path": rel, "total_lines": len(lines), "lines": f"{start}-{end}", "text": "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))}

    def files(self, wid: str, prefix: str) -> dict[str, Any]:
        out = [f for f in self.git(self.wt(wid), "ls-files").splitlines() if f.startswith(prefix) and not is_secret_path(f)]
        return {"count": len(out), "files": out[:200]}

    # --- R2（承認後に実行される） ---
    def push(self, wid: str) -> dict[str, Any]:
        root = self.wt(wid)
        branch = self.branch(root)
        if not branch.startswith("feature/") or branch in PROTECTED:
            raise DevError("feature/* ブランチ以外は push しません")
        self.git(root, "push", "--set-upstream", "origin", f"{branch}:{branch}", timeout=300)  # --force は使わない
        return {"pushed": branch}

    def pr_draft(self, wid: str, title: str, body: str) -> dict[str, Any]:
        root = self.wt(wid)
        branch = self.branch(root)
        if not branch.startswith("feature/") or branch in PROTECTED:
            raise DevError("feature/* ブランチ以外の PR は作りません")
        base = self.repos[wid.split("/")[0]].get("base_ref", "origin/main").removeprefix("origin/")
        r = _run(["gh", "pr", "create", "--draft", "--base", base, "--head", branch, "--title", title, "--body", body], root, 120, self.run)
        if r.returncode != 0:
            raise DevError(f"gh pr create が失敗しました: {(r.stderr or r.stdout).strip()[-300:]}")
        return {"draft_pr": (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""}


def build_tools(cfg: dict[str, Any], dev: Dev | None = None) -> list[ToolSpec]:
    """dev_* ツール群。登録リポジトリ（remote_url つき）が無ければ空。"""
    d = dev or Dev(cfg)
    if not d.repos:
        return []
    wid = {"type": "string", "description": "'repo/slug' 形式の worktree（dev_start の戻り値）"}

    def obj(props: dict[str, Any], req: list[str]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": req, "additionalProperties": False}

    def guard(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def run(a: dict[str, Any]) -> dict[str, Any]:
            try:
                return fn(a)
            except DevError as e:
                return {"error": "dev_error", "reason": str(e)}
            except subprocess.TimeoutExpired:
                return {"error": "timeout", "reason": "時間の上限を超えました"}
            except OSError as e:
                return {"error": "os_error", "reason": type(e).__name__}
        return run

    repo_enum = {"type": "string", "enum": sorted(d.repos)}
    return [
        ToolSpec("dev_start", "登録リポジトリの専用cloneに、feature/<slug> の worktree を作る（作業の最初に1回）。", obj({"repo": repo_enum, "slug": {"type": "string"}}, ["repo", "slug"]),
                 Level.R1, guard(lambda a: d.start(a["repo"], a["slug"]))),
        ToolSpec("dev_files", "worktree のファイル一覧（tracked）。prefix で絞れる。", obj({"worktree": wid, "prefix": {"type": "string"}}, ["worktree"]),
                 Level.R0, guard(lambda a: d.files(a["worktree"], a.get("prefix", "")))),
        ToolSpec("dev_read", "worktree 内のファイルを行番号つきで読む（最大200行）。", obj({"worktree": wid, "path": {"type": "string"}, "start": {"type": "integer", "minimum": 1}, "end": {"type": "integer", "minimum": 1}}, ["worktree", "path"]),
                 Level.R0, guard(lambda a: d.read(a["worktree"], a["path"], int(a.get("start", 1)), int(a.get("end", 200))))),
        ToolSpec("dev_write", "worktree 内のファイルを書く。content で全文、または old/new で1箇所だけ置換。worktree の外・.git・秘匿ファイルは不可。",
                 obj({"worktree": wid, "path": {"type": "string"}, "content": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["worktree", "path"]),
                 Level.R1, guard(lambda a: d.write(a["worktree"], a["path"], a.get("content"), a.get("old"), a.get("new")))),
        ToolSpec("dev_run", "設定済みのビルド/テストを実行する（任意のコマンドは不可）。失敗が上限（5回）に達したら停止して報告する。", obj({"worktree": wid, "kind": {"type": "string", "enum": ["build", "test"]}}, ["worktree", "kind"]),
                 Level.R1, guard(lambda a: d.run_cmd(a["worktree"], a["kind"]))),
        ToolSpec("dev_diff", "worktree の変更（status・stat・diff）を見る。", obj({"worktree": wid}, ["worktree"]), Level.R0, guard(lambda a: d.diff(a["worktree"]))),
        ToolSpec("dev_commit", "worktree の変更を feature ブランチにコミットする（秘匿ファイルが含まれていたら拒否）。", obj({"worktree": wid, "message": {"type": "string", "minLength": 3, "maxLength": 500}}, ["worktree", "message"]),
                 Level.R1, guard(lambda a: d.commit(a["worktree"], a["message"]))),
        ToolSpec("dev_push", "feature ブランチを origin に push する（承認が必要。force・main への push は不可）。", obj({"worktree": wid}, ["worktree"]),
                 Level.R2, guard(lambda a: d.push(a["worktree"])), summarizer=lambda a: f"{a.get('worktree')} のブランチを origin に push します（force なし）"),
        ToolSpec("dev_pr_draft", "PR を下書きで作る（承認が必要）。", obj({"worktree": wid, "title": {"type": "string", "maxLength": 200}, "body": {"type": "string", "maxLength": 5000}}, ["worktree", "title", "body"]),
                 Level.R2, guard(lambda a: d.pr_draft(a["worktree"], a["title"], a["body"])),
                 summarizer=lambda a: f"{a.get('worktree')} から下書きPRを作成します: {a.get('title', '')[:80]}"),
    ]
