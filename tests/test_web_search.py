"""web_search のテスト（ネットワーク不使用）。"""
from __future__ import annotations

from policy.levels import Level
from tools.web_fetch import FetchBlocked
from tools.web_search import build_tool, parse_duckduckgo

DDG = '''
<div class="result"><h2><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1&amp;rut=abc">Example &amp; <b>A</b></a></h2>
<a class="result__snippet" href="x">これは <b>最初</b> の説明です。</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=bing&u=1">広告</a>
<a class="result__snippet" href="x">買って</a></div>
<div class="result"><a class="result__a" href="https://example.org/b">B title</a>
<a class="result__snippet" href="x">B desc</a></div>
'''


def test_parse_duckduckgo_decodes_urls_and_skips_ads():
    r = parse_duckduckgo(DDG, 10)
    assert [x["url"] for x in r] == ["https://example.com/a?x=1", "https://example.org/b"]
    assert r[0]["title"] == "Example & A" and r[0]["snippet"] == "これは 最初 の説明です。"


def test_parse_respects_limit_and_empty():
    assert len(parse_duckduckgo(DDG, 1)) == 1 and parse_duckduckgo("<html></html>", 5) == []


def test_duckduckgo_provider_goes_through_public_resolver():
    seen = {}

    def getter(url, ip, timeout, max_bytes, ua):
        seen.update(url=url, ip=ip)
        return {"status": 200, "headers": {}, "body": DDG.encode(), "truncated": False}

    t = build_tool({"provider": "duckduckgo"}, resolver=lambda h, p: ["1.2.3.4"], getter=getter)
    out = t.handler({"query": "bullet physics 日本語"})
    assert t.level is Level.R0 and out["results"][0]["url"] == "https://example.com/a?x=1"
    assert "bullet%20physics" in seen["url"] and seen["ip"] == "1.2.3.4"


def test_private_resolution_blocks_search():
    def resolver(h, p):
        raise FetchBlocked("非公開")

    out = build_tool({}, resolver=resolver, getter=lambda *a, **k: None).handler({"query": "x"})
    assert out["error"] == "search_failed"


def test_searxng_requires_url_and_uses_fixed_base():
    out = build_tool({"provider": "searxng"}).handler({"query": "x"})
    assert out["error"] == "search_failed" and "searxng_url が未設定" in out["reason"]
    calls = []

    def searx(base, q, limit, timeout):
        calls.append((base, q, limit))
        return [{"title": "T", "url": "https://e.com", "snippet": "s"}]

    t = build_tool({"provider": "searxng", "searxng_url": "http://192.168.0.13:8888"}, searx=searx)
    assert t.handler({"query": "abc", "limit": 3})["results"][0]["title"] == "T"
    assert calls == [("http://192.168.0.13:8888", "abc", 3)]


def test_http_error_status_reported():
    t = build_tool({}, resolver=lambda h, p: ["1.2.3.4"], getter=lambda *a, **k: {"status": 429, "headers": {}, "body": b"", "truncated": False})
    assert t.handler({"query": "x"})["error"] == "search_failed"


WIKI = b'{"query":{"search":[{"title":"Python (programming language)","snippet":"<span class=\\"searchmatch\\">Python</span> is a language"}]}}'


def test_wikipedia_provider_builds_urls_and_goes_through_public_resolver():
    urls = []

    def getter(url, ip, *a, **k):
        urls.append((url, ip))
        return {"status": 200, "headers": {}, "body": WIKI, "truncated": False}

    t = build_tool({"providers": ["wikipedia"], "wikipedia_langs": ["en"]}, resolver=lambda h, p: ["9.9.9.9"], getter=getter)
    out = t.handler({"query": "python"})
    assert out["provider"] == "wikipedia"
    assert out["results"][0]["url"] == "https://en.wikipedia.org/wiki/Python_%28programming_language%29"
    assert out["results"][0]["snippet"] == "Python is a language" and urls[0][1] == "9.9.9.9"


def test_providers_fall_through_in_order_and_report_errors():
    def searx(*a, **k):
        raise OSError("down")

    getter = lambda *a, **k: {"status": 200, "headers": {}, "body": WIKI, "truncated": False}
    t = build_tool({"providers": ["searxng", "wikipedia"], "searxng_url": "http://x", "wikipedia_langs": ["en"]},
                   resolver=lambda h, p: ["9.9.9.9"], getter=getter, searx=searx)
    assert t.handler({"query": "python"})["provider"] == "wikipedia"
    t2 = build_tool({"providers": ["searxng"], "searxng_url": "http://x"}, searx=searx)
    out = t2.handler({"query": "python"})
    assert out["error"] == "search_failed" and "searxng: OSError" in out["reason"]


def test_no_results_everywhere_is_reported_not_invented():
    empty = b'{"query":{"search":[]}}'
    t = build_tool({"providers": ["wikipedia"], "wikipedia_langs": ["en"]}, resolver=lambda h, p: ["9.9.9.9"],
                   getter=lambda *a, **k: {"status": 200, "headers": {}, "body": empty, "truncated": False})
    out = t.handler({"query": "zzzz"})
    assert out["error"] == "search_failed" and "該当なし" in out["reason"]
