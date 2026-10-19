"""Faraway.exe の自己更新の端到端検証: 旧版（0.1.0）を実際にビルドし、本物のサーバーが配布する最新版へ入れ替える。

実行（PowerShell）:  powershell -File apps\\windows\\build.ps1 ; python apps\\windows\\e2e_clientupdate.py
確認すること:
  - 旧版が --self-update --yes で、サーバー配布の新版（SHA-256 一致）に入れ替わり、旧版は .bak に残る
  - 最新の版は何もしない（終了コード 10）
  - SHA-256 の不一致・サイズの不一致・実行ファイルでないものは、入れ替えずに拒否する（終了コード 11）
  - 配布エンドポイントは認証必須
pytest には含めない（Windows・csc・Faraway.exe が必要）。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import api.server as server  # noqa: E402
from api.tokens import TokenStore  # noqa: E402
from core.agent import Agent  # noqa: E402
from core.audit import AuditLog  # noqa: E402
from policy.registry import ToolRegistry  # noqa: E402
from tests.conftest import ScriptedLLM  # noqa: E402

PORT = 8793
tmp = Path(tempfile.mkdtemp(prefix="faraway-e2e-client-"))
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail: object = "") -> bool:
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"), flush=True)
    return bool(cond)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_old_exe(dest: Path) -> None:
    """ソースの Version だけを 0.1.0 にして、実際にビルドする（旧版の代わり）。"""
    src = (ROOT / "apps" / "windows" / "Faraway.cs").read_text(encoding="utf-8")
    assert 'const string Version = "' in src
    old_src = tmp / "FarawayOld.cs"
    old_src.write_text(re.sub(r'const string Version = "[0-9.]+"', 'const string Version = "0.1.0"', src), encoding="utf-8-sig")
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    refs = ["System.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Security.dll", "System.Web.Extensions.dll"]
    r = subprocess.run([str(csc), "/nologo", "/target:winexe", f"/out:{dest}", *[f"/reference:{x}" for x in refs], str(old_src)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


new_exe = ROOT / "dist" / "Faraway.exe"
if not new_exe.is_file():
    sys.exit("先に apps\\windows\\build.ps1 で dist\\Faraway.exe をビルドしてください")

store = TokenStore(tmp / "t.json")
token = store.add("e2e-pc")
agent = Agent(ScriptedLLM([]), ToolRegistry(), AuditLog(tmp / "audit"))
srv = server.make_server(agent, store, "127.0.0.1", PORT)
threading.Thread(target=srv.serve_forever, daemon=True).start()
home = tmp / "home"
home.mkdir()
env = {**os.environ, "FARAWAY_HOME": str(home)}


def run(exe: Path, *args: str) -> int:
    return subprocess.run([str(exe), *args], env=env, timeout=60).returncode


try:
    # --- 配布エンドポイント ---
    info = server.windows_client_info()
    check("server offers the built exe with version/sha/size", info["available"] and info["sha256"] == sha(new_exe) and info["size"] == new_exe.stat().st_size and re.match(r"^\d+\.\d+\.\d+$", info["version"]), info)
    for path in ("/api/client/windows", "/api/client/windows/download"):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5)
            check(f"{path} requires a token", False)
        except urllib.error.HTTPError as e:
            check(f"{path} requires a token", e.code == 401)

    # --- 旧版 → 新版 ---
    app = tmp / "app"
    app.mkdir()
    old = app / "Faraway.exe"
    build_old_exe(old)
    old_sha = sha(old)
    check("old build differs from the new one", old_sha != info["sha256"])
    check("pair (saves url + encrypted token, no GUI)", run(old, "--pair", f"http://127.0.0.1:{PORT}/#token={token}") == 0 and (home / "bridge.token").is_file())
    code = run(old, "--self-update", "--yes")
    check("self-update --yes starts the replacement (exit 0)", code == 0, code)
    replaced = False
    for _ in range(60):
        time.sleep(0.5)
        if old.is_file() and sha(old) == info["sha256"]:
            replaced = True
            break
    check("exe was replaced by the server's exact build (sha256 match)", replaced, sha(old))
    check("previous exe kept as .bak", (app / "Faraway.exe.bak").is_file() and sha(app / "Faraway.exe.bak") == old_sha)
    check("up to date -> nothing to do (exit 10)", run(old, "--self-update", "--yes") == 10)

    # --- 不正な配布物は拒否 ---
    before = sha(old)
    shutil.copy2(app / "Faraway.exe.bak", app / "Faraway.exe")                       # 再び旧版にする
    old_hash = sha(app / "Faraway.exe")
    real_info = server.windows_client_info

    def with_info(**over):
        return lambda: {**real_info(), **over}

    for label, patch in (("wrong sha256", {"sha256": "0" * 64}), ("wrong size", {"size": info["size"] + 1}), ("absurd size", {"size": 100 * 1024 * 1024})):
        server.windows_client_info = with_info(**patch)
        code = run(app / "Faraway.exe", "--self-update", "--yes")
        time.sleep(1.5)
        check(f"refused: {label} (exit 11, exe unchanged)", code == 11 and sha(app / "Faraway.exe") == old_hash, code)
    server.windows_client_info = real_info
    # 実行ファイルでないものを配布された場合
    fake = tmp / "notexe.exe"
    fake.write_bytes(b"#!/bin/sh\necho pwned\n" * 50)
    server.WINDOWS_EXE = fake
    check("refused: served file is not an executable (exit 11, exe unchanged)", run(app / "Faraway.exe", "--self-update", "--yes") == 11 and sha(app / "Faraway.exe") == old_hash)
    server.WINDOWS_EXE = new_exe
finally:
    srv.shutdown()   # テストの exe は --self-update で終了する（常駐しない）ので、プロセスの強制終了はしない（本物の Faraway.exe を止めないため）
    time.sleep(1)
    shutil.rmtree(tmp, ignore_errors=True)

bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad or not results else 0)
