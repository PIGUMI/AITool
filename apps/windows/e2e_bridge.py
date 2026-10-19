"""端末ブリッジの端到端検証: 本物のサーバー(Python) + 本物の Faraway.exe(--headless)。ダミーのフォルダだけを使う。

実行（PowerShell）:  powershell -File apps\\windows\\build.ps1 ; python apps\\windows\\e2e_bridge.py
正常な操作と、悪意のある操作（フォルダ外・リンク・秘匿ファイル・実行ファイルの書き込み等）の両方を、実機で確認する。
pytest には含めない（Windows と Faraway.exe が必要）。テスト用フォルダは Documents の下に作り、終了時に消す。
"""
import os, subprocess, sys, tempfile, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.server import make_server
from api.tokens import TokenStore
from core.agent import Agent
from core.audit import AuditLog
from core.bridge import DeviceBridge
from policy.registry import ToolRegistry
from tests.conftest import ScriptedLLM
from tools import device

EXE = str(ROOT / "dist" / "Faraway.exe")
tmp = Path(tempfile.mkdtemp(prefix="faraway-e2e-", dir=str(Path.home() / "Documents")))
home, docs, work, outside = tmp / "home", tmp / "Docs", tmp / "Work", tmp / "outside"
for d in (home, docs, work, outside):
    d.mkdir()
(docs / "memo.txt").write_text("こんにちは\nFarawayのメモ", encoding="utf-8")
(docs / "sub").mkdir(); (docs / "sub" / "a.md").write_text("# a", encoding="utf-8")
(docs / ".env").write_text("SECRET=1", encoding="utf-8")
(docs / "key.pem").write_text("PEM", encoding="utf-8")
(docs / "bin.dat").write_bytes(b"\x00\x01\x02binary")
(outside / "secret.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
(docs / ".ssh").mkdir(); (docs / ".ssh" / "config").write_text("host x", encoding="utf-8")
# ジャンクション（管理者権限不要）で許可フォルダの外を指すリンクを作る
subprocess.run(["cmd", "/c", "mklink", "/J", str(docs / "link"), str(outside)], capture_output=True)

bridge = DeviceBridge()
reg = ToolRegistry()
for s in device.build_tools(bridge):
    reg.register(s)
audit = AuditLog(tmp / "audit")
agent = Agent(ScriptedLLM([]), reg, audit, bridge=bridge)
store = TokenStore(tmp / "t.json")
token = store.add("e2e-pc")
srv = make_server(agent, store, "127.0.0.1", 8796)
threading.Thread(target=srv.serve_forever, daemon=True).start()

env = {**os.environ, "FARAWAY_HOME": str(home)}
def exe(*args, **kw):
    return subprocess.run([EXE, *args], env=env, capture_output=True, timeout=30, **kw)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(detail)[:160]) if not cond else ""))

# ルート登録（CLI）。広すぎる場所・システム領域は拒否されること
check("add-root Docs (read)", exe("--add-root", "Docs", str(docs)).returncode == 0)
check("add-root Work (rw)", exe("--add-root", "Work", str(work), "rw").returncode == 0)
check("add-root refuses drive root", exe("--add-root", "C", "C:\\").returncode == 3)
check("add-root refuses user profile root", exe("--add-root", "U", os.path.expanduser("~")).returncode == 3)
check("add-root refuses Windows dir", exe("--add-root", "W", os.environ["WINDIR"]).returncode == 3)
check("add-root refuses AppData", exe("--add-root", "A", os.environ["APPDATA"]).returncode == 3)
check("add-root refuses .ssh folder", exe("--add-root", "S", str(docs / ".ssh")).returncode == 3)

# ブリッジ起動（ペアリング URL でトークンを DPAPI 保存 → ヘッドレスで接続）
proc = subprocess.Popen([EXE, f"http://127.0.0.1:8796/#token={token}", "--headless"], env=env)
try:
    for _ in range(60):
        if bridge.online("e2e-pc") and bridge.devices()["e2e-pc"]["roots"]:
            break
        time.sleep(0.5)
    info = bridge.devices().get("e2e-pc", {})
    check("device online with roots", bridge.online("e2e-pc") and {r["name"] for r in info["roots"]} == {"Docs", "Work"}, info)
    check("roots report modes only (no paths)", all(set(r) == {"name", "mode"} for r in info["roots"]) and str(docs) not in str(info))
    check("token not stored in plain text", token.encode() not in (home / "bridge.token").read_bytes() and token not in (home / "url.txt").read_text())

    T = {t.name: t for t in device.build_tools(bridge)}
    def call(name, **a):
        return T[name].handler({"device": "e2e-pc", **a})

    out = call("device_list", root="Docs")
    names = [e["name"] for e in out.get("entries", [])]
    check("list shows normal files", {"memo.txt", "sub", "bin.dat"} <= set(names), out)
    check("list hides secrets and links", not ({".env", "key.pem", ".ssh", "link"} & set(names)), names)
    check("list subfolder", [e["name"] for e in call("device_list", root="Docs", path="sub")["entries"]] == ["a.md"])
    r = call("device_read", root="Docs", path="memo.txt")
    check("read utf-8 japanese", r.get("text", "").replace("\r\n", "\n") == "こんにちは\nFarawayのメモ", r)
    check("read refuses binary", "error" in call("device_read", root="Docs", path="bin.dat"))
    for p in (".env", "key.pem", ".ssh/config", "link/secret.txt", "../outside/secret.txt", "..\\..\\x", "C:/Windows/win.ini", "/Windows/win.ini", "memo.txt:stream"):
        r = call("device_read", root="Docs", path=p)
        check(f"read refused: {p}", "error" in r and "OUTSIDE-SECRET" not in str(r), r)
    check("unknown root refused", "error" in call("device_read", root="Nope", path="memo.txt"))
    check("write to read-only root refused", "error" in call("device_write", root="Docs", path="x.txt", content="x") and not (docs / "x.txt").exists())
    w = call("device_write", root="Work", path="notes/日本語.txt", content="書き込みテスト", mode="create")
    check("write create (creates subfolder)", w.get("bytes") == len("書き込みテスト".encode()) and (work / "notes" / "日本語.txt").read_text(encoding="utf-8") == "書き込みテスト", w)
    check("create refuses existing", "error" in call("device_write", root="Work", path="notes/日本語.txt", content="x", mode="create"))
    check("overwrite works", "error" not in call("device_write", root="Work", path="notes/日本語.txt", content="上書き", mode="overwrite") and (work / "notes" / "日本語.txt").read_text(encoding="utf-8") == "上書き")
    check("append works", "error" not in call("device_write", root="Work", path="notes/日本語.txt", content="+追記", mode="append") and (work / "notes" / "日本語.txt").read_text(encoding="utf-8") == "上書き+追記")
    for p, why in (("evil.exe", "exe"), ("run.bat", "bat"), ("s.ps1", "ps1"), ("a.lnk", "lnk"), ("x.js", "js"), (".env", "secret"), ("id_rsa", "secret"), ("../escape.txt", "escape"), ("CON", "reserved"), ("a/NUL.txt", "reserved")):
        r = call("device_write", root="Work", path=p, content="x")
        check(f"write refused: {p} ({why})", "error" in r, r)
    check("nothing escaped / no executables created", not (tmp / "escape.txt").exists() and not list(work.rglob("*.exe")) and not list(work.rglob("*.bat")) and not (work / "evil.exe").exists())
    # 許可フォルダ内にジャンクションを作られても、辿らない
    subprocess.run(["cmd", "/c", "mklink", "/J", str(work / "jl"), str(outside)], capture_output=True)
    r = call("device_write", root="Work", path="jl/pwn.txt", content="pwn")
    check("write via junction refused", "error" in r and not (outside / "pwn.txt").exists(), r)
    log = (home / "access.log").read_text(encoding="utf-8")
    check("access.log records ops but no content", "device" not in log and "こんにちは" not in log and "書き込みテスト" not in log and "\tread\tDocs\tmemo.txt\tok" in log, log[-300:])
    check("access.log records refusals", "拒否" in log)
    # 許可フォルダを変更すると、再接続せずに反映される
    exe("--add-root", "Docs", str(docs), "rw")
    bridge.hello  # no-op（クライアントの再 hello は GUI 操作時のみ。CLI 変更は次回起動で反映）
finally:
    proc.kill()
    proc.wait(10)
    import shutil
    for j in (docs / "link", work / "jl"):   # ジャンクションは先に外す（リンク先を消さない）
        try:
            os.rmdir(j)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)

bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
