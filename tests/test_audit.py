import gzip
import json
from datetime import datetime, timedelta

from core.audit import JST, MASK, AuditLog


def test_jsonl_one_line_per_event(audit):
    audit.write("request", "r1", request="hi")
    audit.write("tool_call", "r1", tool="host_status", level="R0")
    lines = audit.path_for(datetime.now(JST)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["tool"] == "host_status"


def test_masks_sensitive_keys_and_nested(audit):
    rec = audit.write("tool_call", "r", args={"api_key": "abc", "nested": {"Password": "p", "ok": 1}, "list": [{"token": "t"}]})
    assert rec["args"]["api_key"] == MASK
    assert rec["args"]["nested"] == {"Password": MASK, "ok": 1}
    assert rec["args"]["list"] == [{"token": MASK}]


def test_masks_bearer_kv_and_registered_secret(tmp_path):
    log = AuditLog(tmp_path, secrets=["s3cr3tvalue"])
    rec = log.write("x", "r", a="Authorization: Bearer abc123", b="token=xyz", c="leak s3cr3tvalue here")
    assert "abc123" not in rec["a"] and "xyz" not in rec["b"] and "s3cr3tvalue" not in rec["c"]
    assert "s3cr3tvalue" not in log.path_for(datetime.now(JST)).read_text(encoding="utf-8")


def test_commit_sha_is_not_masked(audit):
    sha = "a" * 40
    assert audit.write("x", "r", sha=sha)["sha"] == sha


def test_compress_old_keeps_content(audit):
    old = datetime.now(JST) - timedelta(days=3)
    p = audit.path_for(old)
    p.write_text('{"event":"old"}\n', encoding="utf-8")
    audit.write("new", "r")
    out = audit.compress_old()
    assert len(out) == 1 and gzip.open(out[0], "rt", encoding="utf-8").read() == '{"event":"old"}\n'
    assert audit.path_for(datetime.now(JST)).exists()
