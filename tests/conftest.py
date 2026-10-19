import json

import pytest

from core.audit import AuditLog
from core.llm import LLMClient


def tool_call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    """あらかじめ決めた assistant メッセージを順に返すモック。"""

    def __init__(self, script):
        self.script, self.seen = list(script), []

    def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self.script.pop(0) if self.script else {"role": "assistant", "content": "終了"}


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit")
