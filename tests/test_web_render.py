"""web_render のテスト（ブラウザは起動しない。コマンドの安全対策と後処理を検証）。"""
from __future__ import annotations

import os
import subprocess

import pytest

from policy.levels import Level
from tools.web_fetch import FetchBlocked
from tools.web_render import build_command, build_tool, find_browser

HTML = "<html><head><title>描画後</title></head><body><div id=a>JSが作った本文</div><script>x()</script></body></html>".encode()


def tool(runner, resolver=lambda h, p: ["93.184.216.34"], **cfg):
    return build_tool(cfg, runner=runner, resolver=resolver, browser="C:/fake/msedge.exe", free_mb=lambda: 99999)


def test_not_registered_without_a_browser(monkeypatch):
    monkeypatch.setattr("tools.web_render.find_browser", lambda c="": None)
    assert build_tool({}) is None


def test_level_and_schema():
    t = tool(lambda c, t: (0, HTML))
    assert t.level is Level.R0 and t.input_schema["required"] == ["url"]


def test_command_pins_host_and_blocks_everything_else():
    cmd = build_command("edge", "https://example.com/p", "example.com", "93.184.216.34", "C:/tmp/p", 6000)
    rule = next(c for c in cmd if c.startswith("--host-resolver-rules="))
    assert rule == "--host-resolver-rules=MAP example.com 93.184.216.34, MAP * ~NOTFOUND , EXCLUDE example.com"
    assert "--no-proxy-server" in cmd and "--headless=new" in cmd and "--dump-dom" in cmd
    assert "--user-data-dir=C:/tmp/p" in cmd and cmd[-1] == "https://example.com/p"


def test_ipv6_is_bracketed():
    cmd = build_command("edge", "https://e.com", "e.com", "2606:2800::1", "p", 1000)
    assert "MAP e.com [2606:2800::1]," in next(c for c in cmd if "host-resolver" in c)


def test_renders_and_extracts_text():
    seen = {}

    def runner(cmd, timeout):
        seen["cmd"], seen["timeout"] = cmd, timeout
        return 0, HTML

    out = tool(runner).handler({"url": "https://example.com/a"})
    assert out["title"] == "描画後" and "JSが作った本文" in out["text"] and "x()" not in out["text"]
    assert out["rendered"] is True and out["source_url"] == "https://example.com/a" and seen["timeout"] == 30.0
    assert any("MAP example.com 93.184.216.34" in c for c in seen["cmd"])


def test_resolver_result_is_what_the_browser_is_pinned_to():
    seen = {}
    tool(lambda c, t: seen.update(cmd=c) or (0, HTML), resolver=lambda h, p: ["1.2.3.4", "5.6.7.8"]).handler({"url": "https://e.com/"})
    assert any("MAP e.com 1.2.3.4," in c for c in seen["cmd"])


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://x/", "javascript:alert(1)", "https://user:pw@example.com/", "chrome://settings"])
def test_bad_schemes_never_launch(url):
    out = tool(lambda c, t: pytest.fail("起動してはいけない")).handler({"url": url})
    assert out["error"] == "blocked"


def test_private_destination_never_launches():
    def resolver(h, p):
        raise FetchBlocked("非公開アドレスのため拒否しました: 192.168.0.1")

    out = tool(lambda c, t: pytest.fail("起動してはいけない"), resolver=resolver).handler({"url": "http://router.example/"})
    assert out["error"] == "blocked" and "非公開" in out["reason"]


def test_timeout_and_failure_reported():
    def slow(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    assert tool(slow).handler({"url": "https://example.com/"})["error"] == "timeout"
    assert tool(lambda c, t: (1, b"")).handler({"url": "https://example.com/"})["error"] == "render_failed"
    assert tool(lambda c, t: (0, b"  ")).handler({"url": "https://example.com/"})["error"] == "render_failed"

    def missing(cmd, timeout):
        raise FileNotFoundError()

    assert tool(missing).handler({"url": "https://example.com/"})["error"] == "render_failed"


def test_profile_dir_is_temporary_and_removed():
    seen = {}

    def runner(cmd, timeout):
        prof = next(c for c in cmd if c.startswith("--user-data-dir="))[len("--user-data-dir="):]
        seen["p"], seen["existed"] = prof, os.path.isdir(prof)
        return 0, HTML

    tool(runner).handler({"url": "https://example.com/"})
    assert seen["existed"] and not os.path.exists(seen["p"])
    tool(lambda c, t: (1, b"")).handler({"url": "https://example.com/"})  # 失敗時も残さない


def test_low_memory_blocks_start():
    t = build_tool({"min_free_memory_mb": 1000}, runner=lambda c, t: pytest.fail("起動してはいけない"), resolver=lambda h, p: ["1.1.1.1"],
                   browser="x", free_mb=lambda: 300)
    assert t.handler({"url": "https://example.com/"})["error"] == "low_memory"


def test_text_is_clipped():
    big = b"<body>" + b"a" * 500 + b"</body>"
    out = tool(lambda c, t: (0, big), max_chars=100).handler({"url": "https://example.com/"})
    assert len(out["text"]) == 100 and out["truncated"] is True


def test_find_browser_prefers_configured_path(tmp_path):
    exe = tmp_path / "b.exe"
    exe.write_text("x")
    assert find_browser(str(exe)) == str(exe)
