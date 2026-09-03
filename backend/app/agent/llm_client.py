"""Configurable LLM client (Anthropic by default).

Env precedence: LLM_BASE_URL/LLM_API_KEY/LLM_MODEL override ANTHROPIC_*.
External calls never run inside a DB transaction — callers read/commit first.
"""
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import anthropic


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: List[Dict] = field(default_factory=list)  # [{id,name,input}]
    stop_reason: Optional[str] = None


class LLMClient:
    def __init__(self):
        self.base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
        self.api_key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        self.model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
            or "claude-sonnet-4-6"
        )
        self._stream = os.environ.get("LLM_STREAMING", "auto").lower()
        self.client = anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url)

    def stream_enabled(self) -> bool:
        """auto → True (native Anthropic streaming); true/false override."""
        if self._stream == "true":
            return True
        if self._stream == "false":
            return False
        return True

    def complete(self, system: str, messages: List[Dict],
                 tools: Optional[List[Dict]] = None, max_tokens: int = 1024) -> LLMResult:
        kwargs = dict(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        resp = self.client.messages.create(**kwargs)
        text_parts, tool_calls = [], []
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", "") == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
        return LLMResult(text="".join(text_parts), tool_calls=tool_calls,
                         stop_reason=getattr(resp, "stop_reason", None))
