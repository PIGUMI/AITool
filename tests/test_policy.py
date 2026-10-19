import pytest

from policy.levels import Disposition, Level, PolicyError, decide
from policy.registry import ToolRegistry, ToolSpec

SCHEMA = {"type": "object", "properties": {}}


def spec(name="t", level=Level.R0, schema=SCHEMA):
    return ToolSpec(name, "d", schema, level, lambda a: {})


def test_decide_each_level():
    assert decide(Level.R0) is Disposition.AUTO
    assert decide(Level.R1) is Disposition.AUTO_LOGGED
    assert decide(Level.R2) is Disposition.REQUIRES_APPROVAL
    assert decide(Level.R3) is Disposition.DENIED


def test_unknown_level_is_denied():
    assert decide(None) is Disposition.DENIED
    assert decide(0) is Disposition.DENIED  # Level ではない整数も拒否側


def test_tool_without_level_cannot_register():
    with pytest.raises(PolicyError):
        ToolRegistry().register(spec(level=None))


def test_r3_cannot_register():
    with pytest.raises(PolicyError):
        ToolRegistry().register(spec(level=Level.R3))


def test_duplicate_and_bad_schema_rejected():
    r = ToolRegistry()
    r.register(spec("a"))
    with pytest.raises(PolicyError):
        r.register(spec("a"))
    with pytest.raises(PolicyError):
        r.register(spec("b", schema={"type": "string"}))


def test_openai_tools_include_level():
    r = ToolRegistry()
    r.register(spec("a", Level.R1))
    t = r.openai_tools()[0]["function"]
    assert t["name"] == "a" and t["description"].startswith("[R1]")
