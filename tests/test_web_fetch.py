"""web_fetch のテスト（ネットワーク不使用。名前解決と取得はモック）。"""
from __future__ import annotations

import pytest

from policy.levels import Level
from tools.web_fetch import FetchBlocked, build_tool, check_public_ip, html_to_text

HTML = (b"<html><head><title> \xe3\x83\x86\xe3\x82\xb9\xe3\x83\x88 </title><script>evil()</script></head>"
        b"<body><h1>Hello</h1><p>one   two</p><style>x{}</style><p>three</p></body></html>")


def resp(status=200, body=HTML, ctype="text/html; charset=utf-8", loc=None, truncated=False):
    h = {"content-type": ctype}
    if loc:
        h["location"] = loc
    return {"status": status, "headers": h, "body": body, "truncated": truncated}


def tool(getter, resolver=lambda h, p: ["93.184.216.34"], **cfg):
    return build_tool(cfg, resolver=resolver, getter=getter)


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.0.13", "10.1.2.3", "172.16.0.1", "169.254.1.1",
                                "100.84.113.46", "::1", "fd00::1", "fe80::1", "0.0.0.0", "::ffff:192.168.0.1", "224.0.0.1"])
def test_non_public_ips_blocked(ip):
    with pytest.raises(FetchBlocked):
        check_public_ip(ip)


def test_public_ip_allowed():
    check_public_ip("93.184.216.34")
    check_public_ip("2606:2800:220:1:248:1893:25c8:1946")


def test_level_is_r0_and_schema_requires_url():
    t = tool(lambda *a, **k: resp())
    assert t.level is Level.R0
    assert t.input_schema["required"] == ["url"]


def test_fetch_extracts_text_with_source():
    r = tool(lambda *a, **k: resp()).handler({"url": "https://example.com/a"})
    assert r["source_url"] == "https://example.com/a"
    assert r["title"] == "テスト"
    assert "evil" not in r["text"] and "x{}" not in r["text"]
    assert "one two" in r["text"] and "three" in r["text"]
    assert r["fetched_at"] and r["truncated"] is False


def test_private_resolution_is_blocked_even_for_public_name():
    from tools.web_fetch import resolve_public
    import socket
    orig = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("192.168.0.4", 80))]
    try:
        r = build_tool({}, resolver=resolve_public, getter=lambda *a, **k: pytest.fail("接続してはいけない")).handler({"url": "http://nas.example.com/"})
    finally:
        socket.getaddrinfo = orig
    assert r["error"] == "blocked"


def test_literal_private_url_blocked():
    from tools.web_fetch import resolve_public
    r = build_tool({}, resolver=resolve_public, getter=lambda *a, **k: pytest.fail("接続してはいけない")).handler({"url": "http://100.84.113.46:8080/health"})
    assert r["error"] == "blocked"


def test_redirect_to_private_is_blocked():
    calls = []

    def resolver(host, port):
        calls.append(host)
        return ["192.168.0.1"] if host == "router.local" else ["93.184.216.34"]

    n = {"i": 0}

    def getter(url, ip, *a, **k):
        n["i"] += 1
        return resp(302, b"", loc="http://router.local/admin")

    r = build_tool({}, resolver=lambda h, p: (_ for _ in ()).throw(FetchBlocked("非公開")) if h == "router.local" else ["93.184.216.34"], getter=getter).handler({"url": "https://example.com/"})
    assert r["error"] == "blocked" and n["i"] == 1


def test_redirect_followed_and_final_url_reported():
    seq = [resp(301, b"", loc="/b"), resp()]
    r = tool(lambda *a, **k: seq.pop(0)).handler({"url": "https://example.com/a"})
    assert r["source_url"] == "https://example.com/b" and r["requested_url"] == "https://example.com/a"


def test_redirect_loop_stops():
    r = tool(lambda *a, **k: resp(302, b"", loc="/x"), max_redirects=3).handler({"url": "https://example.com/"})
    assert r["error"] == "blocked"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "gopher://x/", "https://user:pw@example.com/", "javascript:alert(1)"])
def test_bad_schemes_and_credentials(url):
    r = tool(lambda *a, **k: pytest.fail("接続してはいけない")).handler({"url": url})
    assert r["error"] == "blocked"


def test_text_is_clipped_and_flagged():
    r = tool(lambda *a, **k: resp(body=b"a" * 500, ctype="text/plain"), max_chars=100).handler({"url": "https://example.com/"})
    assert len(r["text"]) == 100 and r["truncated"] is True


def test_network_error_reported_without_raising():
    def getter(*a, **k):
        raise TimeoutError()
    r = tool(getter).handler({"url": "https://example.com/"})
    assert r["error"] == "fetch_failed"


def test_html_to_text_basic():
    title, text = html_to_text("<title>T</title><p>a</p><p>b</p><script>x</script>")
    assert title == "T" and [l for l in text.splitlines() if l] == ["a", "b"]
