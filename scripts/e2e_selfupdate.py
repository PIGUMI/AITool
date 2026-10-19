"""自己更新の端到端検証: 本物のスーパーバイザー + 本物のサーバー + 本物の git（ローカルの bare リポジトリを更新元にする）。

実行（PowerShell）:  python scripts\\e2e_selfupdate.py
確認すること:
  1. 初回インストール（bootstrap）→ スーパーバイザー経由でサーバーが起動する
  2. 更新元に新しい版（v2）→ 確認 → 準備（自己検査・テスト）→ 承認 → 切り替え・再起動 → 新しい版で動く → 適用が確定
  3. 検証は通るが起動に失敗する版（v3）→ 承認して切り替え → 試用期間中に落ちる → 自動で v2 に戻る → v3 は再試行されない
  4. 監査ログ・データが更新をまたいで残る（リリースの外に保存されている）
先に変更をコミットしておくこと（更新元には HEAD を使う）。テスト用の環境は一時フォルダに作り、終了時に消す。pytest には含めない。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.tokens import TokenStore  # noqa: E402
from core.updater import bootstrap  # noqa: E402

PORT = 8794
BASE = f"http://127.0.0.1:{PORT}"
ID = ["-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
tmp = Path(tempfile.mkdtemp(prefix="faraway-e2e-update-"))
results: list[tuple[str, bool]] = []
sup_proc: subprocess.Popen | None = None


def check(name: str, cond: bool, detail: object = "") -> bool:
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"), flush=True)
    return bool(cond)


def git(cwd: Path | str, *args: str) -> str:
    r = subprocess.run(["git", *ID, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr[-300:]}")
    return r.stdout.strip()


token = ""


def api(path: str, body: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}"}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(cond, timeout: float = 120, every: float = 0.7) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(every)
    return False


def version() -> str:
    return api("/api/update/status")[1].get("version", "")


def run_update(target_sha: str, label: str) -> bool:
    """確認 → 準備 → 承認要求 → 承認。承認まで行えたら True。"""
    code, info = api("/api/update/check", {})
    if not check(f"{label}: check says behind with the new commit", code == 200 and info["state"] == "behind" and info["latest"] == target_sha, info):
        return False
    code, _ = api("/api/update/prepare", {})
    check(f"{label}: prepare starts in background (202)", code == 202)
    done = wait(lambda: api("/api/update/status")[1].get("last_prepare", {}).get("state") in ("done", "failed"), timeout=400)
    lp = api("/api/update/status")[1].get("last_prepare") or {}
    if not check(f"{label}: prepare verified (selfcheck + tests)", done and lp.get("state") == "done" and lp["result"]["steps"].get("tests") == "passed", lp):
        return False
    code, ap = api("/api/update/apply", {"sha": target_sha})
    check(f"{label}: apply only creates an approval", code == 200 and "approval_id" in ap and version() != target_sha, ap)
    code, out = api(f"/api/approvals/{ap['approval_id']}/approve", {"hash": ap["hash"]})
    return check(f"{label}: approved -> restart requested", code == 200 and out.get("ok") and out["result"].get("restart_requested"), out)


try:
    # --- 更新元（ローカルの bare）と作業用 clone ---
    bare, work, install, secrets = tmp / "remote.git", tmp / "work", tmp / "install", tmp / "secrets"
    git(tmp, "init", "--bare", "-b", "main", str(bare))
    git(ROOT, "push", "-q", str(bare), "HEAD:refs/heads/main")
    git(tmp, "clone", "-q", str(bare), str(work))
    git(work, "checkout", "-q", "main")
    v1 = git(work, "rev-parse", "HEAD")

    # --- インストール（bootstrap）と、検証用の設定 ---
    secrets.mkdir()
    (secrets / "llama_api_key.txt").write_text("dummy-key", encoding="utf-8")
    token = TokenStore(secrets / "api_tokens.json").add("e2e")
    boot = bootstrap(ROOT, install)
    check("bootstrap: release imported and supervisor installed", boot["release"] == v1[:12] and (install / "supervisor.py").is_file() and (install / "config.toml").is_file(), boot)
    cfg = (install / "config.toml").read_text(encoding="utf-8").replace("\r\n", "\n")
    cfg = re.sub(r"^hosts = .*$", 'hosts = ["127.0.0.1"]', cfg, flags=re.M)
    cfg = re.sub(r"^port = 8765", f"port = {PORT}", cfg, flags=re.M)
    cfg = re.sub(r'^remote_url = "https://github.com/PIGUMI/AITool.git"   #', f'remote_url = "{bare.as_posix()}"   #', cfg, flags=re.M)
    cfg = cfg.replace("auto_check_hours = 6", "auto_check_hours = 0").replace("build_exe = true", "build_exe = false")
    cfg = cfg.replace('test_command = ["python", "-m", "pytest", "-q", "-x"]', 'test_command = ["python", "-m", "pytest", "-q", "-x", "tests/test_policy.py", "tests/test_tools.py"]')
    check("config rewritten for the test", bare.as_posix() in cfg and f"port = {PORT}" in cfg and "tests/test_policy.py" in cfg)
    (install / "config.toml").write_text(cfg, encoding="utf-8")

    # --- スーパーバイザー経由で起動 ---
    env = {**os.environ, "ASSISTANT_SECRETS_DIR": str(secrets), "PYTHONUTF8": "1"}
    sup_log = open(tmp / "supervisor.out", "w", encoding="utf-8")
    sup_proc = subprocess.Popen([sys.executable, str(install / "supervisor.py"), "--install-dir", str(install), "--health-port", str(PORT),
                                 "--stable-seconds", "4"], env=env, stdout=sup_log, stderr=subprocess.STDOUT)
    up = wait(lambda: api("/api/update/status")[0] == 200, timeout=60)
    st = api("/api/update/status")[1] if up else {}
    check("server started under the supervisor", up and st.get("supervised") is True and st.get("version") == v1 and st.get("current_release") == v1[:12], st)
    try:
        urllib.request.urlopen(BASE + "/api/update/status", timeout=5)
        check("update status requires a token", False)
    except urllib.error.HTTPError as e:
        check("update status requires a token", e.code == 401)

    # --- v2: 正常な更新 ---
    (work / "E2E_V2.txt").write_text("v2 marker", encoding="utf-8")
    git(work, "add", "-A"); git(work, "commit", "-q", "-m", "v2: テスト用の変更")
    git(work, "push", "-q", "origin", "HEAD:main")
    v2 = git(work, "rev-parse", "HEAD")
    if run_update(v2, "v2"):
        ok = wait(lambda: version() == v2, timeout=120)
        check("v2: server restarted on the new version", ok, version())
        check("v2: new release really contains the change", (install / "releases" / v2[:12] / "E2E_V2.txt").is_file())
        applied = wait(lambda: (api("/api/update/status")[1].get("last_update") or {}).get("status") == "applied", timeout=60)
        st = api("/api/update/status")[1]
        check("v2: update confirmed after the probation period", applied and not st.get("pending") and st["previous_release"] == v1[:12], st)
        audits = "".join(p.read_text(encoding="utf-8") for p in (install / "data").rglob("audit-*.jsonl"))
        check("audit log lives outside the release and survives the update", all(k in audits for k in ('"update_prepared"', '"update_applied"', '"restarting"')), audits[-400:])
        check("data directory is outside releases", (install / "data").is_dir() and not list((install / "releases" / v2[:12]).rglob("audit-*.jsonl")))

        # --- v3: 検証は通るが、起動に失敗する版 → 自動ロールバック ---
        server_py = work / "api" / "server.py"
        s = server_py.read_text(encoding="utf-8")
        marker = "def main() -> int:\n"
        assert marker in s.replace("\r\n", "\n")
        server_py.write_text(s.replace("\r\n", "\n").replace(marker, marker + '    raise RuntimeError("E2E: broken release")\n', 1), encoding="utf-8", newline="")
        git(work, "add", "-A"); git(work, "commit", "-q", "-m", "v3: 起動に失敗する版（検証は通る）")
        git(work, "push", "-q", "origin", "HEAD:main")
        v3 = git(work, "rev-parse", "HEAD")
        if run_update(v3, "v3(bad)"):
            rolled = wait(lambda: (api("/api/update/status")[1].get("last_update") or {}).get("status") == "rolled_back", timeout=120)
            st = api("/api/update/status")[1]
            check("v3: crash during probation -> automatically rolled back to v2", rolled and st["current_release"] == v2[:12] and st["version"] == v2, st)
            check("v3: failed release is recorded and never retried", v3[:12] in (install / "bad.txt").read_text().split(), st.get("bad"))
            code, _ = api("/api/update/check", {})
            code, body = api("/api/update/prepare", {"sha": v3})
            time.sleep(1)
            lp = api("/api/update/status")[1].get("last_prepare") or {}
            check("v3: prepare refuses the known-bad release", lp.get("state") == "failed" and "再試行しません" in lp.get("error", ""), lp)
            check("server is healthy on v2 after the rollback", api("/api/update/status")[0] == 200)
finally:
    if sup_proc is not None:
        subprocess.run(["taskkill", "/PID", str(sup_proc.pid), "/T", "/F"], capture_output=True)
        try:
            sup_proc.wait(15)
        except Exception:
            pass
    time.sleep(1)
    log = tmp / "supervisor.out"
    failed = [n for n, ok in results if not ok]
    if failed and log.exists():
        print("\n--- supervisor output (tail) ---")
        print(log.read_text(encoding="utf-8", errors="replace")[-1500:])
    shutil.rmtree(tmp, ignore_errors=True)

bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad or not results else 0)
