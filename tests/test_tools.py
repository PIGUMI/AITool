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


# --- Windows 性能カウンターの解析（実機・PowerShell 不要）---
def test_parse_counters_cpu_and_gpu():
    from tools.host_status import parse_counters
    samples = [
        {"n": r"\pc\processor(_total)\% processor time", "i": "_total", "v": 12.345},
        {"n": r"\pc\gpu adapter memory(luid_0x00000000_0x0000eed1_phys_0)\dedicated usage", "i": "luid_0x00000000_0x0000eed1_phys_0", "v": 11606814720},
        {"n": r"\pc\gpu adapter memory(luid_0x00000000_0x00010a20_phys_0)\dedicated usage", "i": "luid_0x00000000_0x00010a20_phys_0", "v": 0},
        {"n": r"\pc\gpu engine(a)\utilization percentage", "i": "pid_1_luid_0x00000000_0x0000eed1_phys_0_eng_0_engtype_3D", "v": 30.0},
        {"n": r"\pc\gpu engine(b)\utilization percentage", "i": "pid_2_luid_0x00000000_0x0000eed1_phys_0_eng_0_engtype_3D", "v": 25.5},
        {"n": r"\pc\gpu engine(c)\utilization percentage", "i": "pid_2_luid_0x00000000_0x0000eed1_phys_0_eng_3_engtype_Compute", "v": 10.0},
    ]
    r = parse_counters(samples, 16384)
    assert r["cpu_percent"] == 12.3
    g = r["gpu"]
    assert g["vram_used_mb"] == 11069 and g["vram_total_mb"] == 16384
    assert g["utilization_percent"] == 55.5          # 3D の合計（Compute とは別グループ）
    assert g["temperature_c"] is None                # 取れない値を作らない


def test_parse_counters_caps_at_100_and_handles_missing():
    from tools.host_status import parse_counters
    busy = [{"n": "gpu engine", "i": f"pid_{i}_luid_0x0_0x1_phys_0_eng_0_engtype_3D", "v": 60.0} for i in range(3)]
    assert parse_counters(busy)["gpu"]["utilization_percent"] == 100.0
    empty = parse_counters([])
    assert empty["cpu_percent"] is None and "vram_used_mb" not in empty["gpu"] and "utilization_percent" not in empty["gpu"]
