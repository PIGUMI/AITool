"""llama.cpp（OpenAI互換API）への接続。ツール呼び出しの方式を差し替えやすいよう、この層に閉じ込める。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class LLMError(Exception):
    """モデルに接続できない、または応答が不正。"""


def _urllib_transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class LLMClient:
    """複数の接続先を順に試す chat/completions クライアント。"""

    def __init__(self, endpoints: list[str], api_key: str, model: str = "qwen3.5-9b",
                 timeout: float = 120, transport: Transport | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        self.endpoints = [e.rstrip("/") for e in endpoints]
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self.extra = dict(extra or {})  # 例: {"chat_template_kwargs": {"enable_thinking": False}}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             tool_choice: str | None = None) -> dict[str, Any]:
        """assistant メッセージ（dict）を返す。tool_choice="required" でツール呼び出しを強制できる。"""
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        payload.update(self.extra)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        failures: list[str] = []
        for base in self.endpoints:
            try:
                data = self._transport(f"{base}/chat/completions", headers, payload, self.timeout)
            except (urllib.error.URLError, OSError, ValueError) as e:
                failures.append(f"{base}: {type(e).__name__}")
                continue
            try:
                choice = data["choices"][0]
                return {**choice["message"], "_finish_reason": choice.get("finish_reason")}
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"{base}: 応答の形式が不正です") from e
        raise LLMError("どの接続先にも繋がりません: " + ", ".join(failures))
