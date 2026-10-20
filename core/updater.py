"""自己更新（セルフアップデート）。

方針: 「人が main にマージしたコードだけ」を、「検証してから」「承認を得て」反映する。
- 更新元は設定（[update] remote_url / branch）で固定したリポジトリの main だけ。モデルが取得先を変えることはできない
- 更新は新しいリリースを releases/<sha12>/ に用意し、検証してから切り替える（稼働中の版は触らない）
    check   … 最新を取得して、現在との差（コミット一覧）を調べる（読み取りのみ）
    prepare … 展開 → 自己検査（設定で組み立てられるか）→ テスト → （EXE があればビルド）。全部通ったものだけ VERIFIED にする
    apply   … VERIFIED の版へ current.txt を切り替えて、再起動を要求する（R2: 承認が必要）
- 切り替え後はスーパーバイザー（core/supervisor.py）が試用期間で見張り、落ちる・応答しないなら自動で前の版に戻す
- 戻せるよう、直前の版は残す（previous.txt）。失敗した版は bad.txt に記録して再試行しない
- スーパーバイザー経由で起動していないサーバー（開発中の直接起動）は、再起動できないので適用を拒否する
- 巻き戻し（古い版への更新）・分岐した履歴・force は扱わない（祖先関係を確認する）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import lifecycle
from .version import ROOT, current_version

SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
DEFAULT_INSTALL = Path.home() / ".assistant" / "faraway"
ENV_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "COMSPEC", "LANG", "PYTHONIOENCODING", "PROGRAMDATA", "WINDIR")


class UpdateError(Exception):
    """利用者に見せてよいエラー。"""


PR_MERGE = re.compile(r"^Merge pull request #(\d+) from \S+")


def _title_of(subject: str, body: str) -> dict[str, Any]:
    """コミットの題名から、人が読める題名と PR 番号を取り出す。「Merge pull request #7 from …」なら、本文の1行目（PR のタイトル）を使う。"""
    m = PR_MERGE.match(subject)
    if not m:
        return {"title": subject, "pr": None}
    first = next((l.strip() for l in body.splitlines() if l.strip()), "")
    return {"title": first or subject, "pr": int(m.group(1))}


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra or {})
    return env


class Updater:
    def __init__(self, cfg: dict[str, Any], audit: Any = None, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 install_dir: Path | None = None, version: Callable[[], str] = current_version, restart: Callable[[], bool] = lifecycle.request_restart,
                 supervised: Callable[[], bool] = lifecycle.supervised) -> None:
        self.cfg, self.audit, self.run, self.version = cfg, audit, run, version
        self.restart, self.is_supervised = restart, supervised
        self.remote = cfg.get("remote_url", "")
        self.branch = cfg.get("branch", "main")
        self.install = Path(install_dir or os.environ.get("FARAWAY_INSTALL_DIR") or cfg.get("install_dir") or DEFAULT_INSTALL)
        self.test_cmd = list(cfg.get("test_command") or ["python", "-m", "pytest", "-q", "-x"])
        if self.test_cmd and self.test_cmd[0] == "python":
            self.test_cmd[0] = sys.executable
        self.test_timeout = float(cfg.get("test_timeout_seconds", 900))
        self.keep = int(cfg.get("keep_releases", 3))
        self.build_exe = bool(cfg.get("build_exe", True))
        self._lock = threading.Lock()
        self.busy = ""
        self.last_prepare: dict[str, Any] | None = None

    # --- 小道具 ---
    @property
    def repo(self) -> Path:
        return self.install / "repo"

    def _rel(self, name: str) -> Path:
        return self.install / "releases" / name

    def _read(self, name: str) -> str:
        p = self.install / name
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""

    def _write(self, name: str, text: str) -> None:
        self.install.mkdir(parents=True, exist_ok=True)
        (self.install / name).write_text(text, encoding="utf-8")

    def _json(self, name: str) -> dict[str, Any]:
        try:
            return json.loads(self._read(name) or "{}")
        except ValueError:
            return {}

    def _note(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            self.audit.write(event, "-", **fields)

    def _git(self, *args: str, timeout: float = 180, check: bool = True) -> str:
        r = self.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                     timeout=timeout, env=_env())
        if check and r.returncode != 0:
            raise UpdateError(f"git {args[0]} が失敗しました: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout if check else str(r.returncode)

    def bad(self) -> list[str]:
        return [x for x in self._read("bad.txt").splitlines() if x]

    # --- リポジトリの準備 ---
    def ensure_repo(self) -> None:
        if not self.remote:
            raise UpdateError("更新元（[update] remote_url）が設定されていません")
        if not (self.repo / ".git").exists():
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            r = self.run(["git", "clone", "--quiet", "--no-checkout", self.remote, str(self.repo)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=600, env=_env())
            if r.returncode != 0:
                raise UpdateError(f"更新元の取得に失敗しました: {(r.stderr or r.stdout).strip()[-300:]}")
        origin = self._git("remote", "get-url", "origin").strip()
        if origin != self.remote:  # 取得先は設定で固定。異なる場合は何も取得しない
            raise UpdateError("更新用リポジトリの origin が設定（remote_url）と異なります。取得を中止しました")

    # --- 確認 ---
    def check(self) -> dict[str, Any]:
        """最新を取得し、現在の版との差を返す（読み取りのみ。稼働中の版は変わらない）。"""
        self.ensure_repo()
        self._git("fetch", "--quiet", "origin", self.branch, timeout=300)
        latest = self._git("rev-parse", f"origin/{self.branch}").strip()
        cur = self.version()
        known = cur != "unknown" and self._git("cat-file", "-e", f"{cur}^{{commit}}", check=False) == "0"
        commits: list[dict[str, str]] = []
        if not known:
            state = "current_unknown"
        elif cur == latest:
            state = "up_to_date"
        elif self._git("merge-base", "--is-ancestor", cur, latest, check=False) == "0":
            state = "behind"
            log = self._git("log", "--format=%H%x1f%an%x1f%ad%x1f%s%x1f%b%x1e", "--date=short", f"{cur}..{latest}")
            for rec in [r for r in log.split("\x1e") if r.strip()][:30]:
                sha, author, date, subject, body = (rec.strip("\n").split("\x1f") + [""] * 5)[:5]
                commits.append({"sha": sha, "author": author, "date": date, "subject": subject, **_title_of(subject, body)})
        elif self._git("merge-base", "--is-ancestor", latest, cur, check=False) == "0":
            state = "ahead"        # 手元の版のほうが新しい（開発中のコミットなど）
        else:
            state = "diverged"     # 履歴が分岐している（自動では更新しない）
        info = {"state": state, "current": cur, "latest": latest, "branch": self.branch, "commits": commits,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "prepared": self.prepared(),
                "note": "更新されるのは、設定した更新元の main にマージ済みのコードだけです。"}
        self._write("status.json", json.dumps(info, ensure_ascii=False))
        self._note("update_checked", state=state, current=cur[:12], latest=latest[:12])
        return info

    def prepared(self) -> list[str]:
        d = self.install / "releases"
        return sorted(p.name for p in d.iterdir() if (p / "VERIFIED").is_file()) if d.is_dir() else []

    def _describe(self, sha: str) -> dict[str, Any]:
        """コミットの内容（PR のタイトル・番号・日時）。更新用リポジトリに無ければ、分かる範囲で返す。"""
        try:
            out = self._git("log", "-1", "--format=%s%x1f%b%x1f%cd%x1f%ct", "--date=format:%Y-%m-%d %H:%M", sha)
            subject, body, date, ts = (out.rstrip("\n").split("\x1f") + ["", "", "", "0"])[:4]
        except UpdateError:
            return {"title": "", "subject": "", "pr": None, "date": "", "ts": 0}
        return {**_title_of(subject, body), "subject": subject, "date": date.strip(), "ts": int(ts) if ts.strip().isdigit() else 0}

    def prepared_details(self) -> list[dict[str, Any]]:
        """検証済みのリリースを、ID ではなく、内容（PR のタイトル）・日時・現在の版との関係で返す。新しい順。
        relation: current=いま動いている版 / newer=現在より新しい（適用できる）/ older=古い（適用できない）/ bad=以前の更新で失敗 / other=それ以外"""
        running, cur, prev, bad = self.version(), self._read("current.txt"), self._read("previous.txt"), set(self.bad())
        out: list[dict[str, Any]] = []
        for name in self.prepared():
            meta = self._json_file(self._rel(name) / "VERIFIED")
            sha = str(meta.get("sha") or "")
            if not sha:
                try:
                    sha = (self._rel(name) / "RELEASE").read_text(encoding="utf-8").strip()
                except OSError:
                    sha = name
            if name == cur:
                rel = "current"
            elif name in bad:
                rel = "bad"
            elif running != "unknown" and self._git("merge-base", "--is-ancestor", running, sha, check=False) == "0":
                rel = "newer"
            elif running != "unknown" and self._git("merge-base", "--is-ancestor", sha, running, check=False) == "0":
                rel = "older"
            else:
                rel = "other"
            out.append({"name": name, "sha": sha, **self._describe(sha), "relation": rel, "is_previous": name == prev,
                        "verified_at": meta.get("at"), "steps": meta.get("steps") or {}})
        return sorted(out, key=lambda r: (r["ts"], r["name"]), reverse=True)   # コミット時刻（秒）の新しい順

    def apply_summary(self, sha: str) -> str:
        """承認画面に出す要約: 何の更新か（PR のタイトル・日時）と、版の ID。"""
        name = (sha or "")[:12]
        try:
            full = (self._rel(name) / "RELEASE").read_text(encoding="utf-8").strip()
        except OSError:
            full = sha
        d = self._describe(full) if full else {"title": "", "pr": None, "date": ""}
        label = (f"PR #{d['pr']}: " if d.get("pr") else "") + (d.get("title") or "（内容を取得できませんでした）")
        return (f"Faraway を更新して再起動します。\n内容: {label}" + (f"（{d['date']}）" if d.get("date") else "") +
                f"\n版: {self.version()[:12]} → {name}（自己検査・テストは通過済み。起動に失敗したら自動で元に戻ります）")

    @staticmethod
    def _json_file(p: Path) -> dict[str, Any]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # --- 準備（展開・検証）---
    def _resolve_target(self, sha: str | None, info: dict[str, Any]) -> str:
        if info["state"] in ("ahead", "diverged"):
            raise UpdateError("現在の版が更新元より新しい、または履歴が分岐しているため、自動では更新できません")
        target = info["latest"]
        if sha:
            if not SHA.match(sha):
                raise UpdateError("sha は 7〜40桁の16進数で指定してください")
            target = self._git("rev-parse", "--verify", f"{sha}^{{commit}}").strip()
            if self._git("merge-base", "--is-ancestor", target, f"origin/{self.branch}", check=False) != "0":
                raise UpdateError(f"{self.branch} に含まれていない版は更新できません（マージ済みのコードだけが対象です）")
        if target == info["current"]:
            raise UpdateError("すでにその版です")
        if target[:12] in self.bad():
            raise UpdateError("この版は以前の更新で失敗したため、再試行しません")
        return target

    def _extract(self, sha: str, dest: Path) -> None:
        zpath = self.install / f".export-{sha[:12]}.zip"
        try:
            self._git("archive", "--format=zip", "-o", str(zpath), sha)
            with zipfile.ZipFile(zpath) as z:
                base = dest.resolve()
                for m in z.namelist():
                    if not (dest / m).resolve().is_relative_to(base):  # zip-slip の防止
                        raise UpdateError("不正なパスを含むアーカイブです")
                z.extractall(dest)
        finally:
            zpath.unlink(missing_ok=True)

    def prepare(self, sha: str | None = None) -> dict[str, Any]:
        """新しい版を展開し、自己検査・テスト（・EXE のビルド）を通す。全部通れば VERIFIED。稼働中の版には触れない。"""
        if not self._lock.acquire(blocking=False):
            raise UpdateError(f"別の更新作業が進行中です（{self.busy}）")
        try:
            self.busy = "確認中"
            info = self.check()
            target = self._resolve_target(sha, info)
            name = target[:12]
            final = self._rel(name)
            if (final / "VERIFIED").is_file():
                return {"prepared": name, "sha": target, "already": True}
            self.busy = f"準備中: {name}"
            tmp = self._rel(f".tmp-{name}")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            logdir = self.install / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            log = logdir / f"prepare-{name}.log"
            steps: dict[str, str] = {}

            def fail(step: str, detail: str) -> None:
                shutil.rmtree(tmp, ignore_errors=True)
                self._note("update_prepare_failed", sha=name, step=step)
                raise UpdateError(f"{step} に失敗しました。この版は適用しません。\n{detail[-600:]}")

            def sh(label: str, cmd: list[str], timeout: float, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
                try:
                    r = self.run(cmd, cwd=str(tmp), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=_env(extra_env))
                    out = (r.stdout or "") + (r.stderr or "")
                    code = r.returncode
                except subprocess.TimeoutExpired:
                    out, code = f"タイムアウト（{int(timeout)}秒）", -1
                except OSError as e:
                    out, code = f"起動できません: {e}", -1
                with open(log, "a", encoding="utf-8") as f:
                    f.write(f"$ {label}\n{out}\n[exit {code}]\n\n")
                return code, out

            try:
                self._extract(target, tmp)
            except UpdateError:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            (tmp / "RELEASE").write_text(target, encoding="utf-8")
            iso = tempfile.mkdtemp(prefix="faraway-prepare-")   # テストは本物のシークレットに触れない隔離環境で動かす
            try:
                cfg_path = self.install / "config.toml"
                extra = {"ASSISTANT_SECRETS_DIR": iso}
                code, out = sh("selfcheck", [sys.executable, "-m", "core.selfcheck"], 120,
                               {**extra, **({"FARAWAY_CONFIG": str(cfg_path)} if cfg_path.is_file() else {})})
                if code != 0:
                    fail("自己検査（設定で組み立てられるか）", out)
                steps["selfcheck"] = "passed"
                if self.cfg.get("require_tests", True):
                    code, out = sh(" ".join(self.test_cmd), self.test_cmd, self.test_timeout, extra)
                    if code != 0:
                        fail("テスト", out)
                    steps["tests"] = "passed"
                else:
                    steps["tests"] = "skipped"
                steps["exe"] = "not_built"
                build = tmp / "apps" / "windows" / "build.ps1"
                if self.build_exe and os.name == "nt" and build.is_file():
                    # 検証済みリリース自身のビルドスクリプトだけに、この1回の実行で実行ポリシーを緩める（環境を絞っているため、
                    # ユーザーの既定の設定が引き継がれず、スクリプトの実行が禁止されることがある）
                    code, out = sh("build Faraway.exe", ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(build)], 300)
                    steps["exe"] = "built" if code == 0 and (tmp / "dist" / "Faraway.exe").is_file() else "failed"   # EXE の失敗は適用を止めない
            finally:
                shutil.rmtree(iso, ignore_errors=True)
            (tmp / "VERIFIED").write_text(json.dumps({"sha": target, "steps": steps, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}), encoding="utf-8")
            shutil.rmtree(final, ignore_errors=True)
            tmp.rename(final)
            self._note("update_prepared", sha=name, steps=steps)
            return {"prepared": name, "sha": target, "steps": steps, "commits": info["commits"]}
        finally:
            self.busy = ""
            self._lock.release()

    def prepare_async(self, sha: str | None = None) -> dict[str, Any]:
        """管理画面用: 準備（数分かかる）をバックグラウンドで始める。経過は status()["last_prepare"] で見る。"""
        if self.busy:
            raise UpdateError(f"別の更新作業が進行中です（{self.busy}）")
        self.last_prepare = {"state": "running", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

        def work() -> None:
            try:
                res = self.prepare(sha)
                self.last_prepare = {"state": "done", "result": res}
            except UpdateError as e:
                self.last_prepare = {"state": "failed", "error": str(e)}
            except Exception as e:  # 想定外でも、状態は残す
                self.last_prepare = {"state": "failed", "error": f"{type(e).__name__}: {e}"}

        threading.Thread(target=work, daemon=True, name="update-prepare").start()
        return self.last_prepare

    # --- 適用・ロールバック（R2。承認後に呼ばれる）---
    def apply(self, sha: str) -> dict[str, Any]:
        if not self.is_supervised():
            raise UpdateError("スーパーバイザー経由で起動していないため、自己更新はできません（python core\\supervisor.py で起動してください。docs/self-update.md）")
        if not SHA.match(sha or ""):
            raise UpdateError("sha を指定してください（update_prepare の戻り値）")
        name = sha[:12]
        if name in self.bad():
            raise UpdateError("この版は以前の更新で失敗したため、再試行しません")
        if not (self._rel(name) / "VERIFIED").is_file():
            raise UpdateError("この版は検証されていません。先に update_prepare を実行してください")
        cur = self._read("current.txt")
        if not cur or not self._rel(cur).is_dir():
            raise UpdateError("現在の版がリリースとして保存されていません（初回は python -m core.updater bootstrap が必要です）")
        if cur == name:
            raise UpdateError("すでにその版で動いています")
        if (self.install / "pending.json").is_file():
            # 承認要求が複数並んでいても、試用期間中に次の切り替えを重ねない（重ねると、直前の更新が失敗扱いになる）
            raise UpdateError("直前の更新が試用期間中です。正常に動き続けて確定してから、次の更新を適用してください")
        # 前向きの更新だけを許す: 動いている版が、適用する版の祖先でなければ拒否する（古い版・分岐した版へは更新では行かない）
        try:
            target = (self._rel(name) / "RELEASE").read_text(encoding="utf-8").strip()
            running = self.version()
            forward = running != "unknown" and self._git("merge-base", "--is-ancestor", running, target, check=False) == "0"
        except (OSError, UpdateError):
            forward = False
        if not forward:
            raise UpdateError("動いている版より新しい版（子孫）ではないため、更新では適用できません。古い版へ戻すときは update_rollback を使ってください")
        self._write("previous.txt", cur)
        self._write("current.txt", name)
        self._write("pending.json", json.dumps({"from": cur, "to": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}))
        self._note("update_applied", sha=name, previous=cur)
        ok = self.restart()
        if not ok:   # 再起動できなければ、切り替えを取り消す
            self._write("current.txt", cur)
            (self.install / "pending.json").unlink(missing_ok=True)
            raise UpdateError("再起動を要求できませんでした。切り替えは取り消しました")
        self._prune()
        return {"applied": name, "previous": cur, "restart_requested": True,
                "note": "数秒後に再起動します。新しい版が起動に失敗したり応答しなければ、自動で前の版に戻ります。"}

    def rollback(self) -> dict[str, Any]:
        if not self.is_supervised():
            raise UpdateError("スーパーバイザー経由で起動していないため、切り戻しはできません")
        prev, cur = self._read("previous.txt"), self._read("current.txt")
        if not prev or not self._rel(prev).is_dir():
            raise UpdateError("戻る先の版がありません")
        self._write("current.txt", prev)
        self._write("previous.txt", cur)
        (self.install / "pending.json").unlink(missing_ok=True)
        self._write("last_update.json", json.dumps({"status": "manual_rollback", "from": cur, "to": prev, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}))
        self._note("update_rollback", to=prev, was=cur)
        if not self.restart():
            self._write("current.txt", cur)
            self._write("previous.txt", prev)
            raise UpdateError("再起動を要求できませんでした。切り戻しは取り消しました")
        return {"rolled_back_to": prev, "restart_requested": True}

    def _prune(self) -> None:
        """古いリリースを消す。current・previous・VERIFIED 済みの新しいものは残す。"""
        d = self.install / "releases"
        keep = {self._read("current.txt"), self._read("previous.txt")}
        olds = sorted((p for p in d.iterdir() if p.is_dir() and p.name not in keep and not p.name.startswith(".tmp-")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[max(0, self.keep - len(keep)):]:
            shutil.rmtree(p, ignore_errors=True)

    def status(self) -> dict[str, Any]:
        return {"version": self.version(), "supervised": self.is_supervised(), "install_dir": str(self.install), "busy": self.busy,
                "current_release": self._read("current.txt"), "previous_release": self._read("previous.txt"),
                "pending": self._json("pending.json") or None, "last_update": self._json("last_update.json") or None,
                "last_check": self._json("status.json") or None, "prepared": self.prepared(), "prepared_details": self.prepared_details(), "bad": self.bad(),
                "last_prepare": self.last_prepare,
                "remote": self.remote, "branch": self.branch}


# --- 初回インストール（今の開発用チェックアウトから、releases 方式へ移行する）---
def bootstrap(source: Path = ROOT, install: Path | None = None) -> dict[str, Any]:
    """source（git のチェックアウト）の HEAD を最初のリリースとして取り込み、supervisor.py と設定をインストール先へ置く。"""
    install = Path(install or os.environ.get("FARAWAY_INSTALL_DIR") or DEFAULT_INSTALL)

    def git(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=_env())
        if r.returncode != 0:
            raise UpdateError(f"git {args[0]} が失敗しました: {r.stderr.strip()[-200:]}")
        return r.stdout

    sha = git("rev-parse", "HEAD").strip()
    dirty = bool(git("status", "--porcelain").strip())
    name = sha[:12]
    rel = install / "releases" / name
    if not rel.is_dir():
        rel.mkdir(parents=True)
        z = install / f".export-{name}.zip"
        git("archive", "--format=zip", "-o", str(z), "HEAD")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(rel)
        z.unlink(missing_ok=True)
        (rel / "RELEASE").write_text(sha, encoding="utf-8")
        (rel / "VERIFIED").write_text(json.dumps({"sha": sha, "steps": {"bootstrap": "imported"}}), encoding="utf-8")
    (install / "current.txt").write_text(name, encoding="utf-8")
    shutil.copy2(rel / "core" / "supervisor.py", install / "supervisor.py")
    cfg = install / "config.toml"
    copied = False
    if not cfg.is_file():   # 設定は更新で上書きされない場所へ（既存の設定があれば、それを引き継ぐ）
        from .config import external_config_path, DEFAULT_PATH
        src = external_config_path() or (source / "config" / "config.toml")
        shutil.copy2(src if src.is_file() else DEFAULT_PATH, cfg)
        copied = True
    return {"install_dir": str(install), "release": name, "supervisor": str(install / "supervisor.py"), "config": str(cfg),
            "config_copied": copied, "dirty_checkout": dirty,
            "start_command": f'python "{install / "supervisor.py"}" --install-dir "{install}"'}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Faraway の自己更新（CLI）")
    ap.add_argument("action", choices=["bootstrap", "status", "check", "prepare"])
    ap.add_argument("--sha", default=None)
    a = ap.parse_args(argv)
    try:
        if a.action == "bootstrap":
            out = bootstrap()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            if out["dirty_checkout"]:
                print("注意: チェックアウトに未コミットの変更があります。取り込まれたのは HEAD（コミット済み）の内容だけです。")
            return 0
        from .config import load_config
        up = Updater(load_config().get("update", {}))
        out = {"status": up.status, "check": up.check, "prepare": lambda: up.prepare(a.sha)}[a.action]()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except UpdateError as e:
        print(f"エラー: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
