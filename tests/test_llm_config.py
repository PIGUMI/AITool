import urllib.error
from pathlib import Path

import pytest

from core.config import load_config
from core.llm import LLMClient, LLMError
from core.secrets import read_secret
from core.slots import SlotBusy, SlotPool

KEY = "K" * 24


def ok(url, headers, payload, timeout):
    return {"choices": [{"message": {"role": "assistant", "content": url}}]}


def test_falls_back_to_next_endpoint():
    def t(url, h, p, to):
        if "lan" in url:
            raise urllib.error.URLError("down")
        return ok(url, h, p, to)

    m = LLMClient(["http://lan/v1", "http://ts/v1"], KEY, transport=t).chat([{"role": "user", "content": "x"}])
    assert m["content"] == "http://ts/v1/chat/completions"


def test_sends_bearer_and_tools_and_hides_key_in_errors():
    seen = {}

    def t(url, h, p, to):
        seen.update(h=h, p=p)
        return ok(url, h, p, to)

    LLMClient(["http://a/v1"], KEY, transport=t).chat([], [{"type": "function"}])
    assert seen["h"]["Authorization"] == f"Bearer {KEY}" and seen["p"]["tools"]

    def down(*a):
        raise OSError("x")

    with pytest.raises(LLMError) as e:
        LLMClient(["http://a/v1"], KEY, transport=down).chat([])
    assert KEY not in str(e.value)


def test_bad_response_shape():
    with pytest.raises(LLMError):
        LLMClient(["http://a/v1"], KEY, transport=lambda *a: {"nope": 1}).chat([])


def test_read_secret(tmp_path):
    (tmp_path / "k.txt").write_text(" abc \n", encoding="utf-8")
    assert read_secret("k.txt", tmp_path) == "abc"
    with pytest.raises(FileNotFoundError) as e:
        read_secret("missing", tmp_path)
    assert "abc" not in str(e.value)


def test_config_has_endpoints_slots_and_no_secret():
    cfg = load_config()
    assert cfg["llm"]["endpoints"] == ["http://192.168.0.13:8080/v1", "http://100.84.113.46:8080/v1"]
    assert cfg["slots"] == {"interactive": 1, "batch": 1} and cfg["agent"]["max_tool_calls"] == 20
    text = (Path(__file__).parent.parent / "config" / "config.toml").read_text(encoding="utf-8")
    import re
    assert not re.search(r"[0-9a-f]{32,}", text)  # 長い16進（キー）が設定に無い


def test_slots_separate_interactive_and_batch():
    pool = SlotPool(1, 1)
    with pool.acquire("batch"):
        with pool.acquire("interactive", timeout=0.1):  # バッチ中でも対話は取れる
            pass
        with pytest.raises(SlotBusy):
            with pool.acquire("batch", timeout=0.1):
                pass
