"""同じ失敗の繰り返しを止める仕組み（実機の 9B が、同じ失敗する dev_write を 20 回以上続けた）のテスト。"""
from __future__ import annotations

import sys

import pytest

from core.agent import REPEAT_HINT_AT, REPEAT_STOP_AT, Agent
from policy.levels import Level
from policy.registry import ToolRegistry, ToolSpec
from tests.conftest import ScriptedLLM, tool_call
from tools.dev import Dev, _nearest_line_hint, build_tools

S = {"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": False}


def reg_with(handler):
    r = ToolRegistry()
    r.register(ToolSpec("flaky", "d", S, Level.R0, handler))
    return r


def call_msg(x=1, cid="c"):
    return {"role": "assistant", "content": "", "tool_calls": [tool_call("flaky", {"x": x}, cid)]}


def test_identical_failures_get_a_hint_then_stop(audit):
    seen = []
    llm = ScriptedLLM([call_msg(cid=f"c{i}") for i in range(10)])
    r = Agent(llm, reg_with(lambda a: seen.append(1) or {"error": "同じ理由で失敗"}), audit, max_tool_calls=40, max_llm_turns=40).run("x")
    assert r.stopped_reason == "repeat_failure" and len(seen) == REPEAT_STOP_AT                 # 5回目で止まる
    assert "停止しました" in r.answer and "flaky" in r.answer
    tool_msgs = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert "hint" not in tool_msgs[REPEAT_HINT_AT - 2] and "同じ呼び出しが 3 回続けて失敗しています" in tool_msgs[REPEAT_HINT_AT - 1]   # 3回目からヒント
    assert any(e["event"] == "repeat_failure" for e in audit.read())


def test_a_different_call_in_between_resets_the_count(audit):
    outcomes = iter([{"error": "e"}, {"error": "e"}, {"ok": 1}, {"error": "e"}, {"error": "e"}, {"error": "e"}])
    llm = ScriptedLLM([call_msg(cid=f"c{i}") for i in range(6)] + [{"role": "assistant", "content": "完了"}])
    r = Agent(llm, reg_with(lambda a: next(outcomes)), audit, max_tool_calls=40, max_llm_turns=40).run("x")
    assert r.stopped_reason == "done"                                                           # 成功を挟めば、数え直す（3連続の失敗で止まらない）


def test_different_arguments_are_not_counted_as_a_repeat(audit):
    llm = ScriptedLLM([call_msg(x=i, cid=f"c{i}") for i in range(8)] + [{"role": "assistant", "content": "完了"}])
    r = Agent(llm, reg_with(lambda a: {"error": "e"}), audit, max_tool_calls=40, max_llm_turns=40).run("x")
    assert r.stopped_reason == "done"                                                           # 引数を変えて試しているなら、止めない


def test_successful_calls_never_trigger_it(audit):
    llm = ScriptedLLM([call_msg(cid=f"c{i}") for i in range(8)] + [{"role": "assistant", "content": "完了"}])
    r = Agent(llm, reg_with(lambda a: {"ok": 1}), audit, max_tool_calls=40, max_llm_turns=40).run("x")
    assert r.stopped_reason == "done"


def test_repeat_count_does_not_carry_over_to_the_next_request(audit):
    ag = Agent(ScriptedLLM([call_msg(cid=f"a{i}") for i in range(4)] + [{"role": "assistant", "content": "一つ目の終わり"}]
                           + [call_msg(cid=f"b{i}") for i in range(4)] + [{"role": "assistant", "content": "二つ目の終わり"}]),
               reg_with(lambda a: {"error": "e"}), audit, max_tool_calls=40, max_llm_turns=40)
    assert ag.run("一つ目").stopped_reason == "done"     # 4回の失敗は、止める回数（5）に届かない
    assert ag.run("二つ目").stopped_reason == "done"     # 前の依頼の4回を持ち越していれば、ここで止まってしまう


# --- dev_write の「見つからない」の手がかり ---
def test_nearest_line_hint():
    content = 'WEEKDAYS = "月火水木金土日"\nx = 1\n'
    hint = _nearest_line_hint(content, 'WEEKDAYS = "月火水木金土"\n')
    assert "1 行目" in hint and "月火水木金土日" in hint
    assert "近い行も見つかりませんでした" in _nearest_line_hint(content, "completely different text\n")


def test_dev_write_no_match_error_points_to_the_closest_line(tmp_path):
    import subprocess
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@e.com"]
    subprocess.run(["git", "-C", str(seed), "checkout", "-q", "-b", "main"], check=True, capture_output=True)
    (seed / "a.py").write_text("def f():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), *ident, "commit", "-q", "-m", "i"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True, capture_output=True)
    cfg = {"work_dir": str(tmp_path / "w"), "repos": [{"name": "p", "remote_url": str(bare), "base_ref": "origin/main", "test": [sys.executable, "-c", "pass"]}]}
    tools = {t.name: t for t in build_tools(cfg, Dev(cfg, free_mb=lambda: 99999))}
    wid = tools["dev_start"].handler({"repo": "p", "slug": "hint-test"})["worktree"]
    out = tools["dev_write"].handler({"worktree": wid, "path": "a.py", "old": "    return 'helo'\n", "new": "    return 'hi'\n"})
    assert out["error"] == "dev_error" and "2 行目" in out["reason"] and "return 'hello'" in out["reason"] and "dev_read" in out["reason"]
