"""BaseLLMClient contract: shared abstraction, back-compat alias (provider-agnostic)."""
import pytest

from app.agent.llm_client import (
    BaseLLMClient, AnthropicClient, LLMClient, LLMResult,
)


def test_llmclient_is_anthropic_alias():
    assert LLMClient is AnthropicClient
    assert issubclass(AnthropicClient, BaseLLMClient)
    assert issubclass(BaseLLMClient, object)
    # the dataclass is untouched by the refactor
    r = LLMResult(text="t", tool_calls=[{"id": "a", "name": "x", "input": {}}])
    assert r.text == "t" and r.stop_reason is None


def test_stream_disabled_shares_emulated_fallback(monkeypatch):
    # _emulated_stream lives on the base class and is reachable via the alias.
    monkeypatch.setattr(LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(LLMClient, "stream_enabled", lambda self: False)
    c = LLMClient()
    c.complete = lambda *a, **k: LLMResult(text="整段", tool_calls=[])
    events = list(c.stream_complete("s", [{"role": "user", "content": "hi"}]))
    assert {"type": "text", "delta": "整段"} in events
