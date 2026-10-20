"""依頼内容に応じた最初のツール呼び出しの強制（9B が記憶・推測で答えるのを防ぐ）のテスト。"""
from __future__ import annotations

import json

from core.agent import Agent
from core.llm import LLMClient
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call

RULES = [{"pattern": "出典|調べて", "tools": ["web_search", "web_fetch", "not_registered"]},
         {"pattern": "今日|何曜日", "tools": ["current_time"]}]


def reg():
    r = ToolRegistry()
    s = {"type": "object", "properties": {}}
    for n in ("web_search", "web_fetch", "current_time", "network"):
        r.register(ToolSpec(n, n, s, Level.R0, lambda a, n=n: {"tool": n}))
    return r


def script():
    return [{"role": "assistant", "content": "", "tool_calls": [tool_call("web_search", {})]},
            {"role": "assistant", "content": "回答"}]


def test_first_turn_is_forced_to_matching_tools_only(audit):
    llm = ScriptedLLM(script())
    Agent(llm, reg(), audit, force_rules=RULES).run("Pythonの出典つきで教えて")
    assert llm.calls[0] == {"tools": ["web_search", "web_fetch"], "tool_choice": "required"}  # 未登録は除外
    assert llm.calls[1]["tool_choice"] is None and "network" in llm.calls[1]["tools"]        # 2回目以降は通常
    assert any(e["event"] == "forced_tools" and e["tools"] == ["web_fetch", "web_search"] for e in audit.read())


def test_no_match_means_no_forcing(audit):
    llm = ScriptedLLM([{"role": "assistant", "content": "雑談です"}])
    Agent(llm, reg(), audit, force_rules=RULES).run("こんにちは")
    assert llm.calls[0]["tool_choice"] is None and len(llm.calls[0]["tools"]) == 4
    assert not any(e["event"] == "forced_tools" for e in audit.read())


def test_multiple_rules_merge(audit):
    llm = ScriptedLLM(script())
    Agent(llm, reg(), audit, force_rules=RULES).run("今日の出典付きニュースを調べて")
    assert set(llm.calls[0]["tools"]) == {"web_search", "web_fetch", "current_time"}


def test_rule_with_only_unregistered_tools_does_not_force(audit):
    llm = ScriptedLLM([{"role": "assistant", "content": "x"}])
    Agent(llm, reg(), audit, force_rules=[{"pattern": "foo", "tools": ["nope"]}]).run("foo")
    assert llm.calls[0]["tool_choice"] is None


def test_llm_client_sends_tool_choice_only_when_given():
    sent = []

    def transport(url, headers, payload, timeout):
        sent.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}

    c = LLMClient(["http://x/v1"], "k", transport=transport)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    c.chat([{"role": "user", "content": "a"}], tools, tool_choice="required")
    c.chat([{"role": "user", "content": "a"}], tools)
    c.chat([{"role": "user", "content": "a"}], None, tool_choice="required")
    assert sent[0]["tool_choice"] == "required" and "tool_choice" not in sent[1] and "tool_choice" not in sent[2]
    assert "k" not in json.dumps(sent)  # APIキーはペイロードに含めない


# --- 計画への同意（「はい」）は、直前に確認があったときだけ、実行の開始として扱う ---
def test_affirmative_reply_forces_dev_start_only_after_a_plan_was_proposed(audit):
    from core.prompts import DEFAULT_FORCE_RULES
    r = ToolRegistry()
    s = {"type": "object", "properties": {}}
    for n in ("dev_start", "search", "current_time"):
        r.register(ToolSpec(n, n, s, Level.R0, lambda a, n=n: {"tool": n}))
    plan = [{"role": "user", "content": "機能を追加して"}, {"role": "assistant", "content": "実装計画: 1. … 2. … この方針で進めますか？"}]
    for text in ("はい", "はい。標準ライブラリだけで進めて", "お願いします", "進めてください", "OK"):
        llm = ScriptedLLM([{"role": "assistant", "content": "了解"}])
        Agent(llm, r, audit, force_rules=DEFAULT_FORCE_RULES).run(text, history=plan)
        assert llm.calls[0] == {"tools": ["dev_start"], "tool_choice": "required"}, text
    for hist in ([], [{"role": "user", "content": "こんにちは"}, {"role": "assistant", "content": "こんにちは。何かご用ですか？"}]):
        llm = ScriptedLLM([{"role": "assistant", "content": "はい"}])
        Agent(llm, r, audit, force_rules=DEFAULT_FORCE_RULES).run("はい", history=hist)
        assert llm.calls[0]["tool_choice"] is None                       # 計画の確認が無かった「はい」は、何も強制しない


def test_history_pattern_rules_ignore_requests_that_do_not_match(audit):
    r = ToolRegistry()
    r.register(ToolSpec("dev_start", "d", {"type": "object", "properties": {}}, Level.R0, lambda a: {}))
    rules = [{"pattern": "^はい", "history_pattern": "進めますか", "tools": ["dev_start"]}]
    llm = ScriptedLLM([{"role": "assistant", "content": "x"}])
    Agent(llm, r, audit, force_rules=rules).run("いいえ", history=[{"role": "assistant", "content": "進めますか？"}])
    assert llm.calls[0]["tool_choice"] is None


def test_prompt_tells_to_execute_after_consent_and_to_prefer_the_standard_library():
    from core.prompts import SYSTEM_PROMPT
    assert "計画や確認を繰り返さず、すぐに dev_start から実行します" in SYSTEM_PROMPT
    assert "zipfile と XML だけで作れます" in SYSTEM_PROMPT and "requirements.txt はありません" in SYSTEM_PROMPT
