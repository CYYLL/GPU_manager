"""LLMClient wraps anthropic; env-configurable; parses text + tool_use blocks."""
from unittest import mock

import pytest

from app.agent.llm_client import LLMClient, LLMResult


class _FakeContent:
    def __init__(self, text=None, tool_calls=None):
        self.content = []
        if text is not None:
            block = mock.Mock()
            block.type = "text"
            block.text = text
            self.content.append(block)
        for tc in (tool_calls or []):
            block = mock.Mock()
            block.type = "tool_use"
            block.id = tc["id"]
            block.name = tc["name"]
            block.input = tc["input"]
            self.content.append(block)


def _fake_response(content):
    resp = mock.Mock()
    resp.content = content.content
    resp.stop_reason = "tool_use" if any(b.type == "tool_use" for b in resp.content) else "end_turn"
    return resp


def test_complete_parses_text_and_tool_use(monkeypatch):
    fake_client = mock.Mock()
    fake_client.messages.create.return_value = _fake_response(
        _FakeContent(text="", tool_calls=[{"id": "t1", "name": "get_gpu_status", "input": {}}])
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake_client)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_MODEL", "m")

    client = LLMClient()
    result = client.complete("sys", [{"role": "user", "content": "hi"}])

    assert result.text == ""
    assert result.tool_calls == [{"id": "t1", "name": "get_gpu_status", "input": {}}]
    # model / key / base_url wired from env
    assert fake_client.messages.create.call_args.kwargs["model"] == "m"


def test_env_defaults_to_anthropic_vars(monkeypatch):
    fake_client = mock.Mock()
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake_client)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://a")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet-1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    client = LLMClient()
    assert client.model == "sonnet-1"


def test_llm_vars_override_anthropic_vars(monkeypatch):
    captured = {}

    class _RecordingAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.messages = mock.Mock()

    monkeypatch.setattr("anthropic.Anthropic", _RecordingAnthropic)
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("LLM_MODEL", "llm-model")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://anthropic")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "anthropic-model")

    client = LLMClient()

    # LLM_* wins for model resolution
    assert client.model == "llm-model"
    # Anthropic is constructed with the resolved LLM_* api_key / base_url
    assert captured["api_key"] == "llm-key"
    assert captured["base_url"] == "http://llm"
    assert captured.get("timeout") == 60.0
    # resolved model is what gets sent on calls (not the ANTHROPIC_* model)
    client.client.messages.create.return_value.content = []
    client.complete("sys", [{"role": "user", "content": "hi"}])
    assert client.client.messages.create.call_args.kwargs["model"] == "llm-model"

