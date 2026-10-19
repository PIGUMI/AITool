from __future__ import annotations

import pytest

from policy.levels import Level
from tools.notify import build_tool, is_private_host


def tool(sent, private=True):
    return build_tool({"url": "http://192.168.0.13:2586", "topic": "assistant"}, sender=lambda u, h, b, t: sent.append((u, h, b)) or 200,
                      private_check=lambda h: private)


def test_not_configured_returns_none():
    assert build_tool({}) is None and build_tool({"url": "http://x"}) is None


def test_send_ok_is_r1_and_encodes_title():
    sent = []
    t = tool(sent)
    out = t.handler({"title": "ビルド結果", "message": "成功しました", "priority": "high"})
    assert t.level is Level.R1 and out == {"sent": True, "http_status": 200}
    url, headers, body = sent[0]
    assert url == "http://192.168.0.13:2586/assistant" and headers["Priority"] == "4"
    assert headers["Title"].isascii() and body.decode() == "成功しました"


@pytest.mark.parametrize("msg", ["token=abc123", "Bearer abcdef", "password: hunter2", "a" * 45, "API_KEY=zzz"])
def test_secret_like_content_is_never_sent(msg):
    sent = []
    out = tool(sent).handler({"message": msg})
    assert out["sent"] is False and out["error"] == "blocked" and sent == []


def test_secret_in_title_is_blocked():
    sent = []
    assert tool(sent).handler({"title": "token=abc", "message": "ok"})["sent"] is False and sent == []


def test_public_destination_is_blocked():
    sent = []
    out = tool(sent, private=False).handler({"message": "こんにちは"})
    assert out["error"] == "blocked" and sent == []


def test_send_failure_reported():
    def boom(*a):
        raise OSError("x")

    t = build_tool({"url": "http://192.168.0.13:2586", "topic": "t"}, sender=boom, private_check=lambda h: True)
    assert t.handler({"message": "x"})["error"] == "send_failed"


def test_private_host_check_literal_ips():
    assert is_private_host("192.168.0.13") and is_private_host("127.0.0.1") and is_private_host("100.84.113.46")
    assert not is_private_host("8.8.8.8")
