from core.schema import validate
from tools import host_status, network
from tools.host_status import Unavailable


def test_host_status_returns_collector_values_unchanged():
    gpu = {"temperature_c": 61, "utilization_percent": 12, "vram_used_mb": 9000, "vram_total_mb": 16384}
    tool = host_status.build_tool({}, collectors={s: (lambda c: gpu) for s in host_status.SECTIONS})
    assert tool.level.name == "R0"
    assert tool.handler({"sections": ["gpu"]}) == {"gpu": {"status": "ok", "data": gpu}}


def test_host_status_unavailable_and_error_are_reported_not_invented():
    def boom(c):
        raise RuntimeError("x")

    def na(c):
        raise Unavailable("未設定")

    tool = host_status.build_tool({}, collectors={**{s: na for s in host_status.SECTIONS}, "disk": boom})
    out = tool.handler({})
    assert out["gpu"]["status"] == "unavailable" and out["disk"]["status"] == "error"
    assert "data" not in out["gpu"]


def test_host_status_schema_rejects_unknown_section():
    tool = host_status.build_tool({})
    assert validate({"sections": ["nope"]}, tool.input_schema)
    assert not validate({"sections": ["gpu"]}, tool.input_schema)


def test_host_status_disk_uses_configured_paths(tmp_path):
    out = host_status.DEFAULT_COLLECTORS["disk"]({"disk_paths": [str(tmp_path)]})
    assert out[str(tmp_path)]["free_bytes"] > 0


CFG = {"timeout_seconds": 1, "devices": [
    {"name": "pc", "address": "10.0.0.1", "enabled": True, "ports": [8080], "web": "http://10.0.0.1/health"},
    {"name": "off", "address": "10.0.0.2", "enabled": False, "ports": []},
    {"name": "blank", "address": "", "enabled": True, "ports": []},
]}


def make(calls):
    return network.build_tool(
        CFG,
        ping=lambda a, t: calls.append(("ping", a)) or True,
        port=lambda a, p, t: calls.append(("port", a, p)) or False,
        web=lambda u, t: calls.append(("web", u)) or {"alive": True, "http_status": 200},
    )


def test_network_only_configured_enabled_devices():
    calls = []
    tool = make(calls)
    assert tool.input_schema["properties"]["device"]["enum"] == ["pc"]
    assert validate({"device": "10.0.0.99"}, tool.input_schema)  # 任意アドレスは指定できない
    assert validate({"device": "off"}, tool.input_schema)
    out = tool.handler({})
    assert list(out) == ["pc"] and out["pc"]["ping"] is True and out["pc"]["ports"] == {"8080": False}
    assert {c[1] for c in calls if c[0] != "web"} == {"10.0.0.1"}


def test_network_check_selection():
    calls = []
    make(calls).handler({"device": "pc", "checks": ["ping"]})
    assert calls == [("ping", "10.0.0.1")]
