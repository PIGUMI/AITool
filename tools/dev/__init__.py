"""dev（R0〜R2）：feature ブランチ・git worktree での開発作業。

- 作業は専用の clone（work_dir/clones/<repo>）とその worktree（work_dir/worktrees/<repo>/<slug>）の中だけ。
  常駐PCのメインのチェックアウトには触れない。ブランチは必ず feature/<slug>。
- ファイル編集は worktree の中に限る（.git・秘匿ファイルは不可）。実行できるのは config に書いた build/test コマンドだけ。
- ビルド・テストの失敗は worktree ごとに数え、上限（既定5回）に達したら止まって報告する。
- push（R2）・PR下書き（R2）は承認が必要。main への push・force push・ブランチ削除は実装しない。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from core.secrets import secrets_dir
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


GITHUB_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
SECRET_LIKE = re.compile(r"(bearer\s+\S+|(token|password|passwd|secret|api[_-]?key)\s*[=:]\s*\S+|\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|[A-Za-z0-9_\-]{40,})", re.I)


def _nearest_line_hint(content: str, old: str) -> str:
    """old の最初の行に一番近い行を教える（手がかりのない失敗を、同じ引数で繰り返させないため）。"""
    first = next((l.strip() for l in old.splitlines() if l.strip()), "")
    lines = content.splitlines()
    near = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.5) if first else []
    if not near:
        return "近い行も見つかりませんでした（ファイルが想定と違うかもしれません）。"
    n = next(i for i, l in enumerate(lines, 1) if l.strip() == near[0])
    return f"最も近い行は {n} 行目: 「{lines[n - 1][:120]}」です。"


def check_syntax(rel: str, text: str) -> None:
    """Python / JSON / TOML は、書き込む前に構文を検査する。壊れていれば DevError（ファイルは書かれない）。"""
    suffix = Path(rel).suffix.lower()
    try:
        if suffix == ".py":
            compile(text, rel, "exec")
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml":
            tomllib.loads(text)
    except SyntaxError as e:
        raise DevError(f"構文エラーのため書き込みませんでした（{rel} の {e.lineno} 行目: {e.msg}）。"
                       "引用符・括弧の閉じ忘れなどを確認して、書き直してください") from None
    except (ValueError, tomllib.TOMLDecodeError) as e:
        raise DevError(f"形式が不正なため書き込みませんでした（{rel}: {str(e)[:120]}）。書き直してください") from None


def looks_secret(text: str) -> bool:
    """トークン・パスワードらしい文字列を含むか。コミット SHA（40桁の16進数）は秘匿情報ではないので除く。"""
    return bool(SECRET_LIKE.search(re.sub(r"\b[0-9a-fA-F]{40}\b", "", text)))


def _safe(fn: Callable[[], str], fallback: str) -> str:
    """承認画面の要約の作成が失敗しても、承認要求そのものは作れるようにする。"""
    try:
        return fn()
    except Exception:
        return fallback


def parse_github(remote_url: str) -> tuple[str, str] | None:
    """GitHub のリモート URL から (owner, repo)。GitHub 以外（ローカルパス等）は None。"""
    m = GITHUB_REMOTE.match(remote_url.strip())
    return (m.group(1), m.group(2)) if m else None


def _http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, Any]:
    """GitHub API 呼び出し。api.github.com 以外には送らない。戻り値は (HTTPステータス, JSON)。"""
    if not url.startswith("https://api.github.com/"):
        raise DevError("api.github.com 以外には送りません")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "null")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise DevError(f"GitHub に接続できません（{type(e).__name__}）") from None


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

    def __init__(self, cfg: dict[str, Any], run: Runner = subprocess.run, free_mb: Callable[[], int | None] | None = None,
                 http: Callable[..., tuple[int, Any]] = _http) -> None:
        self.cfg, self.run, self.http = cfg, run, http
        self.repos = {r["name"]: r for r in cfg.get("repos", []) if r.get("name") and r.get("remote_url")}
        self.work = Path(cfg.get("work_dir") or (Path.home() / ".assistant" / "work"))
        self.max_fail = int(cfg.get("max_fix_attempts", 5))
        self.timeout = float(cfg.get("command_timeout_seconds", 600))
        self.min_free = int(cfg.get("min_free_memory_mb", 1500))
        self.free_mb = free_mb or self._free_mb
        self._fails: dict[str, int] = {}
        self._dirty: dict[str, bool] = {}   # 書き込みのあと、まだテストが通っていない worktree
        self.require_tests = bool(cfg.get("require_tests_before_commit", True))
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
            if cur.count(old) == 0:
                raise DevError("old がファイルの中に見つかりません。" + _nearest_line_hint(cur, old) +
                               "dev_read で現在の内容を確かめ、old を実際の文字列（空白・改行も含めて）に合わせるか、content で全文を書き直してください")
            if cur.count(old) != 1:
                raise DevError(f"old が {cur.count(old)} 箇所に一致しました（ちょうど1箇所になるよう範囲を調整してください）")
            text = cur.replace(old, new)
        check_syntax(rel, text)   # 壊れたファイルは書かない（書き込み前に、その場でモデルへ返す）
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8", newline="")
        self._dirty[wid] = True
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
            if kind == "test" and code == 0:
                self._dirty[wid] = False   # 変更のあとでテストが通った
        return {"kind": kind, "exit_code": code, "passed": code == 0, "failures_so_far": n, "attempts_left": max(0, self.max_fail - n),
                "output_tail": out[-OUT_TAIL:]}

    def commit(self, wid: str, message: str) -> dict[str, Any]:
        root = self.wt(wid)
        branch = self.branch(root)
        if not branch.startswith("feature/") or branch in PROTECTED:
            raise DevError("feature/* ブランチ以外ではコミットしません")
        if self.require_tests and self.repos.get(wid.split("/")[0], {}).get("test") and self._dirty.get(wid):
            raise DevError("変更を加えたあと、テストが通っていません。dev_run の test を実行し、通ってからコミットしてください"
                           "（失敗したら原因を直します。直せなければ、止まって報告します）")
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

    # --- push と PR の作成を1回の承認で（GitHub API）---
    def head(self, wid: str) -> str:
        return self.git(self.wt(wid), "rev-parse", "HEAD").strip()

    def github_token(self) -> str | None:
        """GitHub のトークン（secrets の github_token.txt。権限は PR の作成だけに絞ったものを想定）。コード・ログ・モデルには出さない。"""
        p = secrets_dir() / str(self.cfg.get("github_token_file", "github_token.txt"))
        return p.read_text(encoding="utf-8").strip() or None if p.is_file() else None

    def submit_preview(self, a: dict[str, Any]) -> str:
        """承認画面に出す要約: ブランチ・コミット数・変更ファイル（差分の統計）・PR のタイトルと本文の冒頭。"""
        wid = a["worktree"]
        root = self.wt(wid)
        repo = self.repos[wid.split("/")[0]]
        base = repo.get("base_ref", "origin/main")
        branch = self.branch(root)
        try:
            n = self.git(root, "rev-list", "--count", f"{base}..HEAD").strip()
            stat = self.git(root, "diff", "--stat", f"{base}...HEAD")[-1500:].strip()
        except DevError:
            n, stat = "?", "（差分を取得できませんでした）"
        return (f"ブランチ {branch}（{n} コミット）を origin に push し、{base.removeprefix('origin/')} 宛ての"
                f"{'下書き' if self.cfg.get('pr_draft', True) else ''}PR を作成します（マージは GitHub で人が行います）。\n"
                f"タイトル: {a.get('title', '')}\n本文（冒頭）: {str(a.get('body', ''))[:300]}\n変更:\n{stat}\n（HEAD {a.get('expect_head', '')[:12]}）")

    def submit(self, wid: str, title: str, body: str, expect_head: str | None = None) -> dict[str, Any]:
        """feature ブランチを push し、下書き PR を作る。承認のあとでコミットが変わっていたら、何もしない。"""
        root = self.wt(wid)
        branch = self.branch(root)
        if not branch.startswith("feature/") or branch in PROTECTED:
            raise DevError("feature/* ブランチ以外は push しません")
        head = self.head(wid)
        if expect_head and head != expect_head:
            raise DevError(f"承認のあとに worktree の内容が変わりました（承認時 {expect_head[:12]} → 現在 {head[:12]}）。もう一度 dev_submit を依頼してください")
        if looks_secret(title) or looks_secret(body):
            raise DevError("タイトル・本文に、秘匿情報らしい文字列が含まれるため、送りません")
        repo = self.repos[wid.split("/")[0]]
        base = repo.get("base_ref", "origin/main").removeprefix("origin/")
        out: dict[str, Any] = {"pushed": self.push(wid)["pushed"], "head": head}
        gr = str(repo.get("github_repo", ""))   # "owner/name" を明示できる（省略時は remote_url から読み取る）
        gh = (gr.split("/", 1)[0], gr.split("/", 1)[1]) if re.match(r"^[\w.-]+/[\w.-]+$", gr) else parse_github(repo["remote_url"])
        if gh is None:
            return {**out, "pull_request": None, "note": "GitHub のリポジトリではないため、PR は作成していません"}
        owner, name = gh
        compare = f"https://github.com/{owner}/{name}/compare/{base}...{branch}?expand=1"
        token = self.github_token()
        if not token:
            return {**out, "pull_request": None, "compare_url": compare,
                    "note": "GitHub のトークンが未設定のため、PR は作成していません。compare_url から、GitHub 上で PR を作成してください（docs/setup-github.md）"}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "faraway-assistant", "Content-Type": "application/json"}
        payload = {"title": title[:200], "head": branch, "base": base, "draft": bool(self.cfg.get("pr_draft", True)),
                   "body": body[:5000] + "\n\n---\n🤖 Faraway が作成した PR です。内容を確認してからマージしてください。"}
        api = f"https://api.github.com/repos/{owner}/{name}"
        status, data = self.http("POST", f"{api}/pulls", headers, json.dumps(payload).encode(), 20)
        if status == 201:
            return {**out, "pull_request": data.get("html_url"), "number": data.get("number")}
        if status == 422:  # 同じブランチの PR がすでにある場合は、それを返す
            st, items = self.http("GET", f"{api}/pulls?head={owner}:{branch}&state=open", headers, None, 20)
            if st == 200 and isinstance(items, list) and items:
                return {**out, "pull_request": items[0].get("html_url"), "number": items[0].get("number"), "note": "すでに PR があります"}
        msg = str(data.get("message", ""))[:150] if isinstance(data, dict) else ""
        return {**out, "pull_request": None, "compare_url": compare,
                "note": f"push は完了しましたが、PR を作成できませんでした（GitHub API: HTTP {status} {msg}）。compare_url から作成してください"}


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
        ToolSpec("dev_push", "feature ブランチを origin に push する（承認が必要。force・main への push は不可）。通常は dev_submit を使う。", obj({"worktree": wid}, ["worktree"]),
                 Level.R2, guard(lambda a: d.push(a["worktree"])), summarizer=lambda a: f"{a.get('worktree')} のブランチを origin に push します（force なし）", egress=True),
        ToolSpec("dev_pr_draft", "PR を下書きで作る（gh コマンドが必要。承認が必要）。通常は dev_submit を使う。", obj({"worktree": wid, "title": {"type": "string", "maxLength": 200}, "body": {"type": "string", "maxLength": 5000}}, ["worktree", "title", "body"]),
                 Level.R2, guard(lambda a: d.pr_draft(a["worktree"], a["title"], a["body"])),
                 summarizer=lambda a: f"{a.get('worktree')} から下書きPRを作成します: {a.get('title', '')[:80]}", egress=True),
        ToolSpec("dev_submit", "変更を GitHub に出す: feature ブランチを push し、下書き PR を作る（1回の承認）。承認画面に差分の要約が出る。承認後にコミットが変わると実行されない。マージは人が GitHub で行う。タイトルと本文には、何を・なぜ変えたか、テスト結果を書く。",
                 obj({"worktree": wid, "title": {"type": "string", "minLength": 3, "maxLength": 200}, "body": {"type": "string", "minLength": 3, "maxLength": 5000}}, ["worktree", "title", "body"]),
                 Level.R2, guard(lambda a: d.submit(a["worktree"], a["title"], a["body"], a.get("expect_head"))),
                 summarizer=lambda a: _safe(lambda: d.submit_preview(a), f"{a.get('worktree')} を push し、下書き PR「{a.get('title', '')[:80]}」を作成します"),
                 bind=lambda a: {"expect_head": d.head(a["worktree"])}, egress=True),
    ]
