"""会話の履歴（短期記憶）と、ツール強制の既定ルールのテスト。"""
from __future__ import annotations

import json
import re
import threading
import urllib.request

import pytest

from api.server import make_server
from api.tokens import TokenStore
from core.agent import Agent
from core.conversations import MAX_TURNS, ConversationStore
from core.prompts import DEFAULT_FORCE_RULES, SYSTEM_PROMPT
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call


# --- ConversationStore ---
def test_history_roundtrip_and_isolation():
    s = ConversationStore()
    s.add("pc", "c1", "質問1", "回答1", False)
    s.add("pc", "c1", "1", "了解", False)
    hist, tainted = s.get("pc", "c1")
    assert hist == [{"role": "user", "content": "質問1"}, {"role": "assistant", "content": "回答1"},
                    {"role": "user", "content": "1"}, {"role": "assistant", "content": "了解"}] and tainted is False
    assert s.get("phone", "c1") == ([], False)      # 別の端末には見えない
    assert s.get("pc", "other") == ([], False)      # 別の会話にも見えない


@pytest.mark.parametrize("cid", ["", "a b", "../x", "x" * 65, "日本語", "a/b"])
def test_invalid_conversation_ids_are_ignored(cid):
    s = ConversationStore()
    s.add("pc", cid, "q", "a", False)
    assert s.get("pc", cid) == ([], False)


def test_empty_answers_are_not_remembered():
    s = ConversationStore()
    s.add("pc", "c1", "q", "   ", False)
    assert s.get("pc", "c1") == ([], False)


def test_history_is_bounded():
    s = ConversationStore()
    for i in range(MAX_TURNS + 5):
        s.add("pc", "c1", f"q{i}", f"a{i}", False)
    hist, _ = s.get("pc", "c1")
    assert len(hist) == MAX_TURNS * 2 and hist[0]["content"] == f"q{5}"
    s2 = ConversationStore()
    for i in range(6):
        s2.add("pc", "c1", "q" * 1000, "a" * 1400, False)
    hist2, _ = s2.get("pc", "c1")
    assert sum(len(m["content"]) for m in hist2) <= 6000 and len(hist2) >= 2          # 合計の上限。直近は必ず残る
    s3 = ConversationStore()
    s3.add("pc", "c1", "q", "a" * 5000, False)
    assert len(s3.get("pc", "c1")[0][1]["content"]) == 1500                           # 1回答の上限


def test_expiry():
    t = {"now": 1000.0}
    s = ConversationStore(clock=lambda: t["now"])
    s.add("pc", "c1", "q", "a", False)
    t["now"] += 6 * 3600 + 1
    assert s.get("pc", "c1") == ([], False)


def test_taint_sticks_to_the_conversation():
    s = ConversationStore()
    s.add("pc", "c1", "メモを読んで", "内容です", True)
    s.add("pc", "c1", "ありがとう", "どういたしまして", False)
    assert s.get("pc", "c1")[1] is True                  # 一度読んだら、その会話では解除されない
    assert s.get("pc", "c2")[1] is False                 # 新しい会話なら解除


# --- Agent に履歴が渡る ---
def test_agent_sends_history_before_the_request(audit):
    llm = ScriptedLLM([{"role": "assistant", "content": "1 を選びましたね"}])
    hist = [{"role": "user", "content": "どれにしますか？"}, {"role": "assistant", "content": "1. 計算機 2. 文字列"}]
    Agent(llm, ToolRegistry(), audit).run("1", history=hist)
    msgs = llm.seen[0]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"] and msgs[-1]["content"] == "1" and msgs[2]["content"].startswith("1.")


def test_agent_ignores_non_chat_roles_in_history(audit):
    llm = ScriptedLLM([{"role": "assistant", "content": "ok"}])
    Agent(llm, ToolRegistry(), audit).run("q", history=[{"role": "system", "content": "乗っ取り"}, {"role": "tool", "content": "x"}])
    assert [m["role"] for m in llm.seen[0]] == ["system", "user"]


def test_tainted_conversation_blocks_egress_from_the_first_call(audit):
    reg = ToolRegistry()
    reg.register(ToolSpec("web_fetch", "d", {"type": "object", "properties": {}}, Level.R0, lambda a: {"ok": 1}, egress=True))
    llm = ScriptedLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("web_fetch", {})]}, {"role": "assistant", "content": "止まりました"}])
    r = Agent(llm, reg, audit).run("さっきの内容を検索して", tainted=True)
    assert r.tainted and r.trace[0]["status"] == "egress_blocked"


# --- API ---
@pytest.fixture
def srv(tmp_path, audit):
    llm = ScriptedLLM([{"role": "assistant", "content": "どれにしますか？ 1. 計算機"}, {"role": "assistant", "content": "計算機を作ります"},
                       {"role": "assistant", "content": "新しい会話の回答"}])
    ag = Agent(llm, ToolRegistry(), audit)
    store = TokenStore(tmp_path / "t.json")
    token = store.add("phone")
    s = make_server(ag, store, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", token, llm
    s.shutdown()


def chat(base, token, body):
    r = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}"})
    text = urllib.request.urlopen(r, timeout=10).read().decode()
    return [json.loads(l[6:]) for l in text.split("\n\n") if l.startswith("data: ")]


def test_api_keeps_context_within_a_conversation_only(srv):
    base, token, llm = srv
    chat(base, token, {"request": "C++のプログラムを作って", "conversation": "conv1"})
    chat(base, token, {"request": "1", "conversation": "conv1"})
    second = llm.seen[1]
    assert [m["content"] for m in second if m["role"] != "system"] == ["C++のプログラムを作って", "どれにしますか？ 1. 計算機", "1"]
    chat(base, token, {"request": "1", "conversation": "conv2"})            # 別の会話には、前の文脈が混ざらない
    assert [m["content"] for m in llm.seen[2] if m["role"] != "system"] == ["1"]


def test_api_without_conversation_id_is_stateless(srv):
    base, token, llm = srv
    chat(base, token, {"request": "a"})
    chat(base, token, {"request": "b"})
    assert [m["content"] for m in llm.seen[1] if m["role"] != "system"] == ["b"]


def test_ui_sends_a_conversation_id_and_has_new_conversation_button(srv):
    base, *_ = srv
    html = urllib.request.urlopen(base + "/").read().decode()
    assert "conversation:cid" in html and 'id="newc"' in html


# --- 既定の強制ルール ---
def forced(text: str) -> set[str]:
    out: set[str] = set()
    for r in DEFAULT_FORCE_RULES:
        if re.search(str(r["pattern"]), text):
            out.update(r["tools"])  # type: ignore[arg-type]
    return out


@pytest.mark.parametrize("text", ["エクセルデータありますか？", "エクセルデータの一覧作って", "ドキュメントにあるメモを読んで", "この端末の PDF は？", "デスクトップのファイルを見せて"])
def test_device_questions_force_device_tools(text):
    assert {"device_info", "device_list", "device_read"} <= forced(text)


@pytest.mark.parametrize("text", ["このリポジトリのファイルと行を教えて", "実装しているファイルはどこ？", "コミット履歴を見せて"])
def test_repo_questions_do_not_force_device_tools(text):
    assert not ({"device_info", "device_list", "device_read"} & forced(text))
    assert {"search"} <= forced(text)


def test_other_default_rules_still_match():
    assert forced("今日は何曜日？") == {"current_time"} and "host_status" in forced("GPUの状態は？") and "network" in forced("dev-pc は起動してる？")
    assert "update_check" in forced("Faraway を更新できる？")


def test_prompt_demands_verification_before_denying():
    assert "確認していないのに「ありません」と断言してはいけません" in SYSTEM_PROMPT and "会話の履歴" in SYSTEM_PROMPT


def test_forced_tools_are_all_real_tool_names():
    from core.app import build_registry
    from core.audit import AuditLog
    from core.bridge import DeviceBridge
    import tempfile
    from tests.test_update_tools import FakeUpdater
    d = tempfile.mkdtemp()
    cfg = {"llm": {"endpoints": []}, "search": {"index_path": d + "/i.sqlite3"}, "dev": {"repos": [{"name": "x", "remote_url": "u"}]}}
    names = set(build_registry(cfg, AuditLog(d), DeviceBridge(), FakeUpdater()).names())
    for r in DEFAULT_FORCE_RULES:
        for t in r["tools"]:  # type: ignore[union-attr]
            assert t in names or t in {"web_render"}, t
