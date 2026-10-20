"""Faraway のスーパーバイザー（見張り役）。標準ライブラリだけで書かれた単独のファイル。

役割:
- インストール先（既定 %USERPROFILE%\\.assistant\\faraway）の releases/<sha12>/ のうち、current.txt が指す版でサーバー（python -m api.server）を起動する
- サーバーが終了コード 75（更新の適用）で終了したら、current.txt を読み直して、新しい版で起動し直す
- 更新直後は「試用期間」: 新しい版が一定時間（stable_seconds）正常に動き続け、ヘルスチェックに応答したら「適用完了」。
  途中で落ちた・応答しない場合は、自動で前の版（previous.txt）に戻し、その版を bad.txt に記録して再試行しない
- 通常のクラッシュは、間隔を空けて再起動する（短時間に続けて落ちるなら止まる）

自己更新の本体（core/updater.py）は、新しい版の用意・検証・current.txt の切り替えまでを行う。
このファイルは更新の対象にしない想定（インストール時に install_dir/supervisor.py へコピーされ、手動でだけ更新する）。

使い方:  python supervisor.py [--install-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

RESTART_EXIT_CODE = 75
DEFAULT_INSTALL = Path.home() / ".assistant" / "faraway"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Supervisor:
    def __init__(self, install_dir: Path, server_cmd: list[str] | None = None, health_url: str = "http://127.0.0.1:8765/api/health",
                 stable_seconds: float = 30.0, health_deadline: float = 120.0, max_crashes: int = 5, crash_window: float = 120.0,
                 backoff: float = 5.0, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] | None = None) -> None:
        self.dir = Path(install_dir)
        self.cmd = server_cmd or [sys.executable, "-m", "api.server"]
        self.health_url, self.stable, self.deadline = health_url, stable_seconds, health_deadline
        self.max_crashes, self.crash_window, self.backoff = max_crashes, crash_window, backoff
        self.sleep = sleep
        self._log = log or (lambda m: print(m, flush=True))
        self.stop_requested = False
        self.dir.mkdir(parents=True, exist_ok=True)

    # --- 状態ファイル ---
    def _read(self, name: str) -> str:
        p = self.dir / name
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""

    def _write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, encoding="utf-8")

    def _json(self, name: str) -> dict[str, Any]:
        try:
            return json.loads(self._read(name) or "{}")
        except ValueError:
            return {}

    def log(self, msg: str) -> None:
        line = f"{now()} {msg}"
        self._log(line)
        try:
            with open(self.dir / "supervisor.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def current(self) -> str:
        return self._read("current.txt")

    def release_dir(self, name: str) -> Path:
        return self.dir / "releases" / name

    # --- ヘルスチェック ---
    def healthy(self) -> bool:
        """何らかの HTTP 応答があれば生きている（認証なしの /api/health は 401 を返す）。"""
        try:
            urllib.request.urlopen(self.health_url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # --- ロールバック ---
    def rollback(self, reason: str) -> bool:
        pend = self._json("pending.json")
        bad, prev = pend.get("to", self.current()), pend.get("from") or self._read("previous.txt")
        with open(self.dir / "bad.txt", "a", encoding="utf-8") as f:
            f.write(bad + "\n")
        if not prev or not self.release_dir(prev).is_dir():
            self.log(f"ROLLBACK 失敗: 戻る先の版がありません（{reason}）")
            self._write("last_update.json", json.dumps({"status": "rollback_failed", "from": prev, "to": bad, "reason": reason, "at": now()}))
            (self.dir / "pending.json").unlink(missing_ok=True)
            return False
        self._write("current.txt", prev)
        (self.dir / "pending.json").unlink(missing_ok=True)
        self._write("last_update.json", json.dumps({"status": "rolled_back", "from": prev, "to": bad, "reason": reason, "at": now()}))
        self.log(f"ROLLBACK: {bad} → {prev}（{reason}）")
        return True

    # --- 1回分の起動 ---
    def run_once(self) -> tuple[str, int | None]:
        """現在の版でサーバーを起動し、終了まで見張る。戻り値: (結果, 終了コード)。
        結果: "restart"（更新の適用）/ "rolled_back" / "exited"（通常終了）/ "crashed" / "stopped"（スーパーバイザー停止）/ "missing"（版が無い）"""
        cur = self.current()
        rel = self.release_dir(cur) if cur else None
        if not cur or not rel or not rel.is_dir():
            self.log(f"現在の版が見つかりません: {cur!r}")
            return "missing", None
        pend = self._json("pending.json")
        probation = bool(pend) and pend.get("to") == cur
        data = self.dir / "data"   # 監査ログ・ジョブ等。リリースの外に置き、更新で失われないようにする
        data.mkdir(exist_ok=True)
        env = {**os.environ, "FARAWAY_SUPERVISED": "1", "FARAWAY_INSTALL_DIR": str(self.dir), "FARAWAY_DATA_DIR": str(data)}
        cfg = self.dir / "config.toml"
        if cfg.is_file() and "FARAWAY_CONFIG" not in env:
            env["FARAWAY_CONFIG"] = str(cfg)
        self.log(f"起動: {cur}" + ("（更新の試用期間）" if probation else ""))
        proc = subprocess.Popen(self.cmd, cwd=str(rel), env=env)
        started = time.monotonic()
        confirmed = not probation
        while proc.poll() is None:
            if self.stop_requested:
                proc.terminate()
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return "stopped", proc.returncode
            elapsed = time.monotonic() - started
            if not confirmed:
                if elapsed >= self.stable and self.healthy():
                    confirmed = True
                    (self.dir / "pending.json").unlink(missing_ok=True)
                    self._write("last_update.json", json.dumps({"status": "applied", "from": pend.get("from"), "to": cur, "at": now()}))
                    self.log(f"更新を確認しました（{self.stable:.0f} 秒以上正常）: {cur}")
                elif elapsed >= self.deadline:
                    proc.kill()
                    proc.wait(10)
                    self.rollback(f"{self.deadline:.0f} 秒以内にヘルスチェックに応答しませんでした")
                    return "rolled_back", proc.returncode
            self.sleep(0.5)
        code = proc.returncode
        if not confirmed:  # 試用期間中に終了した（再起動要求を含む）= 新しい版の失敗
            self.rollback(f"試用期間中に終了しました（終了コード {code}）")
            return "rolled_back", code
        if code == RESTART_EXIT_CODE:
            return "restart", code
        return ("exited" if code == 0 else "crashed"), code

    # --- メインループ ---
    def run(self) -> int:
        crashes: list[float] = []
        while not self.stop_requested:
            result, code = self.run_once()
            if result in ("stopped",):
                return 0
            if result == "missing":
                return 2
            if result in ("restart", "rolled_back"):
                self.log(f"再起動します（{result}）")
                continue
            if result == "exited":
                self.log("サーバーが正常終了しました。スーパーバイザーも終了します")
                return 0
            t = time.monotonic()
            crashes = [c for c in crashes if t - c < self.crash_window] + [t]
            self.log(f"サーバーが異常終了しました（終了コード {code}）。{len(crashes)}/{self.max_crashes} 回")
            if len(crashes) >= self.max_crashes:
                self.log("短時間に続けて落ちたため、再起動を止めます")
                return 1
            self.sleep(self.backoff)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Faraway スーパーバイザー")
    ap.add_argument("--install-dir", default=os.environ.get("FARAWAY_INSTALL_DIR") or str(DEFAULT_INSTALL))
    ap.add_argument("--health-port", type=int, default=0, help="ヘルスチェックのポート（省略すると設定の [api] port）")
    ap.add_argument("--stable-seconds", type=float, default=30.0)
    a = ap.parse_args(argv)
    install = Path(a.install_dir)
    port = a.health_port
    if not port:
        try:
            import tomllib
            cfgp = install / "config.toml"
            if not cfgp.is_file():
                cfgp = Path(os.environ.get("FARAWAY_CONFIG", "")) if os.environ.get("FARAWAY_CONFIG") else Path.home() / ".assistant" / "config.toml"
            port = int(tomllib.loads(cfgp.read_text(encoding="utf-8"))["api"]["port"]) if cfgp.is_file() else 8765
        except (OSError, ValueError, KeyError, ImportError):
            port = 8765
    sup = Supervisor(install, health_url=f"http://127.0.0.1:{port}/api/health", stable_seconds=a.stable_seconds)
    try:
        return sup.run()
    except KeyboardInterrupt:
        sup.stop_requested = True
        return 0


if __name__ == "__main__":
    sys.exit(main())
