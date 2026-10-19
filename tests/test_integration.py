"""実機（llama.cpp）に接続するテスト。ぴぐみさんのPCで ASSISTANT_INTEGRATION=1 を付けて実行する。"""
import os

import pytest

from core.app import build_agent

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("ASSISTANT_INTEGRATION") != "1", reason="ASSISTANT_INTEGRATION=1 のときだけ実行")]


def test_gpu_question_uses_host_status():
    agent = build_agent()
    r = agent.run("GPUの状態は？")
    assert r.stopped_reason == "done" and r.answer
    assert any(t["tool"] == "host_status" for t in r.trace)
    assert any(e["tool"] == "host_status" for e in agent.audit.read() if e["event"] == "tool_call")
