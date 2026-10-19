"""設定から Agent 一式を組み立てる。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from policy.registry import ToolRegistry
from tools import audit_search, calendar, clock, host_status, network, notify, repo, search, web_fetch, web_search

from .agent import Agent
from .audit import AuditLog
from .config import load_config
from .llm import LLMClient
from .secrets import read_secret
from .slots import SlotPool


def llm_extra(cfg: dict[str, Any]) -> dict[str, Any]:
    """config の [llm.request] をリクエストに足す項目として返す。"""
    return dict(cfg["llm"].get("request", {}))


def build_registry(cfg: dict[str, Any], audit: AuditLog | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(clock.build_tool())
    reg.register(repo.build_read_tool(cfg.get("search", {})))
    reg.register(repo.build_git_tool(cfg.get("search", {})))
    if audit is not None:
        reg.register(audit_search.build_tool(audit))
    reg.register(host_status.build_tool(cfg.get("host_status", {}), cfg["llm"]["endpoints"]))
    reg.register(network.build_tool(cfg.get("network", {})))
    reg.register(web_fetch.build_tool(cfg.get("web", {})))
    reg.register(search.build_tool(cfg.get("search", {})))
    reg.register(web_search.build_tool(cfg.get("web_search", {})))
    for spec in calendar.build_tools(cfg.get("calendar", {})):  # Google 認証情報が無ければ登録しない
        reg.register(spec)
    notify_spec = notify.build_tool(cfg.get("notify", {}))  # ntfy の url/topic が未設定なら登録しない
    if notify_spec is not None:
        reg.register(notify_spec)
    return reg


def build_agent(config_path: Path | str | None = None, llm: LLMClient | None = None) -> Agent:
    cfg = load_config(config_path)
    audit = AuditLog(cfg["audit"]["directory"])
    if llm is None:
        key = read_secret(cfg["llm"]["api_key_file"])
        audit.add_secret(key)
        llm = LLMClient(cfg["llm"]["endpoints"], key, cfg["llm"].get("model", "qwen3.5-9b"),
                        cfg["llm"].get("timeout_seconds", 120), extra=llm_extra(cfg))
    slots = SlotPool(cfg["slots"]["interactive"], cfg["slots"]["batch"])
    return Agent(llm, build_registry(cfg, audit), audit, cfg["agent"]["max_tool_calls"], cfg["agent"]["max_llm_turns"], slots,
                 force_rules=cfg["agent"].get("force_tools", []), approval_ttl=cfg["agent"].get("approval_ttl_seconds", 600))
