"""設定から Agent 一式を組み立てる。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from policy.registry import ToolRegistry
from tools import audit_search, calendar, clock, dev, device, host_status, network, notify, repo, search, update, web_fetch, web_render, web_search

from .agent import Agent
from .audit import AuditLog
from .bridge import DeviceBridge
from .config import data_path, load_config
from .llm import LLMClient
from .prompts import DEFAULT_FORCE_RULES
from .secrets import read_secret
from .slots import SlotPool
from .updater import Updater


def llm_extra(cfg: dict[str, Any]) -> dict[str, Any]:
    """config の [llm.request] をリクエストに足す項目として返す。"""
    return dict(cfg["llm"].get("request", {}))


def build_registry(cfg: dict[str, Any], audit: AuditLog | None = None, bridge: DeviceBridge | None = None,
                   updater: Updater | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    if updater is not None:  # 更新元（[update] remote_url）が設定されているときだけ
        for spec in update.build_tools(updater):
            reg.register(spec)
    if bridge is not None:  # 端末のファイル操作（アプリ限定。ブラウザ版には無い）
        for spec in device.build_tools(bridge):
            reg.register(spec)
    reg.register(clock.build_tool())
    reg.register(repo.build_read_tool(cfg.get("search", {})))
    reg.register(repo.build_git_tool(cfg.get("search", {})))
    if audit is not None:
        reg.register(audit_search.build_tool(audit))
    reg.register(host_status.build_tool(cfg.get("host_status", {}), cfg["llm"]["endpoints"]))
    reg.register(network.build_tool(cfg.get("network", {})))
    reg.register(web_fetch.build_tool(cfg.get("web", {})))
    reg.register(search.build_tool(cfg.get("search", {})))
    render_spec = web_render.build_tool(cfg.get("web_render", {}))  # Edge/Chrome が見つからなければ登録しない
    if render_spec is not None:
        reg.register(render_spec)
    reg.register(web_search.build_tool(cfg.get("web_search", {})))
    for spec in calendar.build_tools(cfg.get("calendar", {})):  # Google 認証情報が無ければ登録しない
        reg.register(spec)
    for spec in dev.build_tools(cfg.get("dev", {})):  # 登録リポジトリ（remote_url つき）が無ければ登録されない
        reg.register(spec)
    notify_spec = notify.build_tool(cfg.get("notify", {}))  # ntfy の url/topic が未設定なら登録しない
    if notify_spec is not None:
        reg.register(notify_spec)
    return reg


def build_agent(config_path: Path | str | None = None, llm: LLMClient | None = None) -> Agent:
    cfg = load_config(config_path)
    audit = AuditLog(data_path(cfg["audit"]["directory"]))   # リリースの外（FARAWAY_DATA_DIR）に置く
    if llm is None:
        key = read_secret(cfg["llm"]["api_key_file"])
        audit.add_secret(key)
        llm = LLMClient(cfg["llm"]["endpoints"], key, cfg["llm"].get("model", "qwen3.5-9b"),
                        cfg["llm"].get("timeout_seconds", 120), extra=llm_extra(cfg))
    try:  # GitHub のトークンがあれば、ログに値が出ないよう、マスク対象に加える（値は読み込むだけ。ツール側で使う）
        audit.add_secret(read_secret(cfg.get("dev", {}).get("github_token_file", "github_token.txt")))
    except FileNotFoundError:
        pass
    slots = SlotPool(cfg["slots"]["interactive"], cfg["slots"]["batch"])
    bridge = DeviceBridge()
    ucfg = cfg.get("update", {})
    updater = Updater(ucfg, audit) if ucfg.get("remote_url") else None
    return Agent(llm, build_registry(cfg, audit, bridge, updater), audit, cfg["agent"]["max_tool_calls"], cfg["agent"]["max_llm_turns"], slots,
                 force_rules=DEFAULT_FORCE_RULES + list(cfg["agent"].get("force_tools", [])), approval_ttl=cfg["agent"].get("approval_ttl_seconds", 600),
                 bridge=bridge, updater=updater)
