"""stream_complete yields text deltas + tool_use blocks; degrades to single-chunk emulation."""
from unittest import mock

from app.agent.llm_client import LLMClient, LLMResult


def _sdk_event(kind, **kw):
    ev = mock.Mock()
    ev.type = kind
    if kind == "content_block_start":
        block = mock.Mock()
        block.type = kw["block_type"]
        if block.type == "tool_use":
            block.id = kw.get("id"); block.name = kw.get("name"); block.input = {}
        ev.block = block
    elif kind == "content_block_delta":
        d = mock.Mock()
        d.type = kw["delta_type"]
        if d.type == "text_delta":
            d.text = kw.get("text", "")
        elif d.type == "input_json_delta":
            d.partial_json = kw.get("partial_json", "")
        ev.delta = d
    return ev


def _mk_stream(events):
    cm = mock.MagicMock()
    cm.__enter__.return_value = iter(events)
    return cm


def test_stream_text_and_tool_use(monkeypatch):
    sdk = mock.Mock()
    sdk.messages.stream.return_value = _mk_stream([
        _sdk_event("content_block_start", block_type="text"),
        _sdk_event("content_block_delta", delta_type="text_delta", text="你好"),
        _sdk_event("content_block_delta", delta_type="text_delta", text="！"),
        _sdk_event("content_block_start", block_type="tool_use", id="t9", name="get_gpu_status"),
        _sdk_event("content_block_delta", delta_type="input_json_delta", partial_json='{"x":'),
        _sdk_event("content_block_delta", delta_type="input_json_delta", partial_json='1}'),
        _sdk_event("content_block_stop"),
    ])
    monkeypatch.setattr(LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(LLMClient, "stream_enabled", lambda self: True)
    c = LLMClient()
    c.model = "m"
    c.client = sdk

    events = list(c.stream_complete("sys", [{"role": "user", "content": "hi"}]))

    assert {"type": "text", "delta": "你好"} in events
    assert {"type": "text", "delta": "！"} in events
    tool = [e for e in events if e["type"] == "tool_use"]
    assert len(tool) == 1 and tool[0]["name"] == "get_gpu_status"
    assert tool[0]["input"] == {"x": 1}


def test_stream_disabled_emulates_single_chunk(monkeypatch):
    monkeypatch.setattr(LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(LLMClient, "stream_enabled", lambda self: False)
    c = LLMClient()
    c.complete = mock.Mock(return_value=LLMResult(
        text="整段回复", tool_calls=[{"id": "a", "name": "x", "input": {}}]))
    events = list(c.stream_complete("sys", [{"role": "user", "content": "hi"}]))
    assert {"type": "text", "delta": "整段回复"} in events
    assert {"type": "tool_use", "id": "a", "name": "x", "input": {}} in events
