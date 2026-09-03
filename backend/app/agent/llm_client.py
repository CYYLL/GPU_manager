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
        self.client = anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url,
                                          timeout=60.0)

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

    def stream_complete(self, system, messages, tools=None, max_tokens=1024):
        """Yield events: {'type':'text','delta'} and {'type':'tool_use','id','name','input'}.

        If stream_enabled() is False, emulate by calling complete() once and yielding
        the whole reply as one text delta plus tool calls — the SSE client logic
        does not fork between real and emulated streaming.
        """
        if not self.stream_enabled():
            result = self.complete(system, messages, tools, max_tokens=max_tokens)
            if result.text:
                yield {"type": "text", "delta": result.text}
            for tc in result.tool_calls:
                yield {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
            return

        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        import json as _json
        with self.client.messages.stream(**kwargs) as stream:
            tool_acc = None  # {'id','name','input_json'} accumulating partial_json
            for event in stream:
                et = getattr(event, "type", "")
                if et == "content_block_start":
                    block = getattr(event, "content_block", None) or getattr(event, "block", None)
                    if getattr(block, "type", "") == "tool_use":
                        tool_acc = {"id": block.id, "name": block.name, "input_json": ""}
                elif et == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dt = getattr(delta, "type", "")
                    if dt == "text_delta":
                        yield {"type": "text", "delta": getattr(delta, "text", "")}
                    elif dt == "input_json_delta" and tool_acc is not None:
                        tool_acc["input_json"] += getattr(delta, "partial_json", "")
                elif et == "content_block_stop" and tool_acc is not None:
                    try:
                        inp = _json.loads(tool_acc["input_json"] or "{}")
                    except Exception:
                        inp = {}
                    yield {"type": "tool_use", "id": tool_acc["id"],
                           "name": tool_acc["name"], "input": inp}
                    tool_acc = None
