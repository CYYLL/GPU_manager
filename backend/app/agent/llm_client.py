"""Configurable LLM clients behind one abstraction (Anthropic default, OpenAI optional).

Provider chosen by LLM_PROVIDER:
  - anthropic (default) -> AnthropicClient (back-compat alias LLMClient): env LLM_*/ANTHROPIC_*
  - openai            -> OpenAIClient (openai_client.py): env OPENAI_*
Env precedence (anthropic path): LLM_BASE_URL/LLM_API_KEY/LLM_MODEL override ANTHROPIC_*.
BaseLLMClient owns the shared params (api_key/base_url/model/streaming/timeout/
max_retries) and the common emulated-stream fallback; each subclass implements
complete()/stream_complete() for its own protocol and returns the neutral shapes
agent_loop consumes. External calls never run inside a DB transaction.
"""
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, List, Dict, Optional

import anthropic


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: List[Dict] = field(default_factory=list)  # [{id,name,input}]
    stop_reason: Optional[str] = None


class BaseLLMClient(ABC):
    """Provider-agnostic parameter set + streaming contract.

    Subclasses resolve their own env vars, then call
    super().__init__(api_key, base_url, model, streaming, timeout, max_retries)
    and build their SDK client.
    """

    def __init__(self, api_key: Optional[str], base_url: Optional[str],
                 model: Optional[str], streaming: str = "auto",
                 timeout: float = 60.0, max_retries: int = 2):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._stream = (streaming or "auto").lower()
        self.timeout = timeout
        self.max_retries = max_retries

    def stream_enabled(self) -> bool:
        """auto → True (native streaming); true/false override."""
        if self._stream == "true":
            return True
        if self._stream == "false":
            return False
        return True

    def _emulated_stream(self, system: str, messages: List[Dict],
                         tools: Optional[List[Dict]] = None, max_tokens: int = 1024):
        """Stream-off fallback shared by all providers: one complete() call,
        replayed as a single text delta plus one tool_use per call."""
        result = self.complete(system, messages, tools, max_tokens=max_tokens)
        if result.text:
            yield {"type": "text", "delta": result.text}
        for tc in result.tool_calls:
            yield {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}

    @abstractmethod
    def complete(self, system: str, messages: List[Dict],
                 tools: Optional[List[Dict]] = None, max_tokens: int = 1024) -> LLMResult:
        """One non-streaming turn -> LLMResult."""

    @abstractmethod
    def stream_complete(self, system: str, messages: List[Dict],
                        tools: Optional[List[Dict]] = None,
                        max_tokens: int = 1024) -> Iterator[dict]:
        """Yield events: {'type':'text','delta'} / {'type':'tool_use','id','name','input'}."""


class AnthropicClient(BaseLLMClient):
    """Anthropic Messages API (also any Anthropic-compatible gateway, e.g. Ark Coding Plan)."""

    def __init__(self):
        super().__init__(
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            base_url=os.environ.get("LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"),
            model=(os.environ.get("LLM_MODEL")
                   or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                   or "claude-sonnet-4-6"),
            streaming=os.environ.get("LLM_STREAMING", "auto"),
        )
        self.client = anthropic.Anthropic(
            api_key=self.api_key, base_url=self.base_url,
            timeout=self.timeout, max_retries=self.max_retries)

    def complete(self, system, messages, tools=None, max_tokens=1024) -> LLMResult:
        kwargs = dict(model=self.model, max_tokens=max_tokens,
                      system=system, messages=messages)
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
        if not self.stream_enabled():
            yield from self._emulated_stream(system, messages, tools, max_tokens=max_tokens)
            return
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
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
                        inp = json.loads(tool_acc["input_json"] or "{}")
                    except Exception:
                        inp = {}
                    yield {"type": "tool_use", "id": tool_acc["id"],
                           "name": tool_acc["name"], "input": inp}
                    tool_acc = None


# Back-compat alias: existing imports/tests use LLMClient for the anthropic client.
LLMClient = AnthropicClient


def create_llm_client() -> BaseLLMClient:
    """Build the client selected by LLM_PROVIDER (default 'anthropic').

    Unknown values fail fast at construction time. The openai module is imported
    lazily so its dependency is only required when that provider is selected.
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicClient()
    if provider == "openai":
        from .openai_client import OpenAIClient  # lazy import
        return OpenAIClient()
    raise ValueError("LLM_PROVIDER must be 'anthropic' or 'openai', got %r" % provider)
