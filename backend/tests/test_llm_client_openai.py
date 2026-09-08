"""OpenAIClient: env resolution, Anthropic<->OpenAI conversion, complete()."""
import json
from unittest import mock

import pytest

from app.agent.llm_client import LLMResult, create_llm_client
from app.agent.openai_client import OpenAIClient, DEFAULT_OPENAI_BASE_URL


def _set_env(monkeypatch, **over):
    base = {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "m",
            "OPENAI_BASE_URL": "http://openai.local/v1", "OPENAI_STREAMING": "auto"}
    base.update(over)
    for key, value in base.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _mk_openai(monkeypatch, create_retval=None):
    sdk = mock.Mock()
    if create_retval is not None:
        sdk.chat.completions.create.return_value = create_retval
    monkeypatch.setattr("openai.OpenAI", lambda **kw: sdk)
    return sdk


def _choice(content, tool_calls=None, finish_reason="stop"):
    tc_mocks = []
    for tc in (tool_calls or []):
        t = mock.Mock()
        t.id = tc["id"]
        t.function = mock.Mock()
        t.function.name = tc["name"]
        t.function.arguments = tc["arguments"]
        tc_mocks.append(t)
    msg = mock.Mock()
    msg.content = content
    msg.tool_calls = tc_mocks
    choice = mock.Mock()
    choice.message = msg
    choice.finish_reason = finish_reason
    return choice


def _response(*choices):
    resp = mock.Mock()
    resp.choices = list(choices)
    return resp


def test_openai_requires_key_and_model(monkeypatch):
    _set_env(monkeypatch, OPENAI_API_KEY=None)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIClient()
    _set_env(monkeypatch)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        OpenAIClient()


def test_openai_ignores_llm_env_and_defaults_base_url(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "anthropic-key")  # must NOT satisfy openai
    monkeypatch.setenv("LLM_MODEL", "anthropic-model")
    _set_env(monkeypatch, OPENAI_BASE_URL=None, OPENAI_API_KEY="openai-key",
             OPENAI_MODEL="openai-model")
    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    client = OpenAIClient()
    assert client.api_key == "openai-key"
    assert client.model == "openai-model"
    assert captured["base_url"] == DEFAULT_OPENAI_BASE_URL
    assert captured["api_key"] == "openai-key"
    assert captured["timeout"] == 60.0 and captured["max_retries"] == 2


def test_complete_converts_tools_and_messages_and_parses(monkeypatch):
    _set_env(monkeypatch)
    sdk = _mk_openai(monkeypatch, create_retval=_response(
        _choice("好的", tool_calls=[{"id": "c1", "name": "get_gpu_status",
                                    "arguments": '{"a": 1}'}],
                finish_reason="tool_calls")))

    client = OpenAIClient()
    result = client.complete(
        "你是助手",
        [{"role": "user", "content": "hi"}],
        [{"name": "get_gpu_status", "description": "查卡",
          "input_schema": {"type": "object", "properties": {}}}])

    called = sdk.chat.completions.create.call_args.kwargs
    assert called["model"] == "m"
    assert called["messages"][0] == {"role": "system", "content": "你是助手"}
    assert called["messages"][1:] == [{"role": "user", "content": "hi"}]
    assert called["tools"] == [{"type": "function", "function": {
        "name": "get_gpu_status", "description": "查卡",
        "parameters": {"type": "object", "properties": {}}}}]
    assert result.text == "好的"
    assert result.tool_calls == [{"id": "c1", "name": "get_gpu_status", "input": {"a": 1}}]
    assert result.stop_reason == "tool_calls"


def test_to_openai_messages_splits_tool_blocks():
    out = OpenAIClient._to_openai_messages([
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_gpu_status",
             "input": {"all": True}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "4 卡空闲"}]},
        {"role": "user", "content": "好"},
    ])
    assert out == [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "get_gpu_status",
                                      "arguments": json.dumps({"all": True},
                                                              ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "4 卡空闲"},
        {"role": "user", "content": "好"},
    ]


def test_complete_bad_tool_json_defaults_to_empty_input(monkeypatch):
    _set_env(monkeypatch)
    _mk_openai(monkeypatch, create_retval=_response(
        _choice("", tool_calls=[{"id": "x", "name": "f", "arguments": "not-json{"}])))
    result = OpenAIClient().complete("s", [{"role": "user", "content": "hi"}])
    assert result.tool_calls == [{"id": "x", "name": "f", "input": {}}]


def test_no_tools_arg_when_none(monkeypatch):
    _set_env(monkeypatch)
    sdk = _mk_openai(monkeypatch, create_retval=_response(_choice("无工具")))
    OpenAIClient().complete("s", [{"role": "user", "content": "hi"}], tools=None)
    called = sdk.chat.completions.create.call_args.kwargs
    assert "tools" not in called


def test_create_llm_client_selects_openai(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    _mk_openai(monkeypatch)  # no network at construction
    client = create_llm_client()
    assert isinstance(client, OpenAIClient)
