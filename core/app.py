"""設定から Agent 一式を組み立てる。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from policy.registry import ToolRegistry
from tools import host_status, network

from .agent import Agent
from .audit import AuditLog
from .config import load_config
from .llm import LLMClient
from .secrets import read_secret
from .slots import SlotPool


def build_registry(cfg: dict[str, Any]) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(host_status.build_tool(cfg.get("host_status", {}), cfg["llm"]["endpoints"]))
    reg.register(network.build_tool(cfg.get("network", {})))
    return reg


def build_agent(config_path: Path | str | None = None, llm: LLMClient | None = None) -> Agent:
    cfg = load_config(config_path)
    audit = AuditLog(cfg["audit"]["directory"])
    if llm is None:
        key = read_secret(cfg["llm"]["api_key_file"])
        audit.add_secret(key)
        llm = LLMClient(cfg["llm"]["endpoints"], key, cfg["llm"].get("model", "qwen3.5-9b"),
                        cfg["llm"].get("timeout_seconds", 120))
    slots = SlotPool(cfg["slots"]["interactive"], cfg["slots"]["batch"])
    return Agent(llm, build_registry(cfg), audit, cfg["agent"]["max_tool_calls"], cfg["agent"]["max_llm_turns"], slots)
