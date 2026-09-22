"""Common tool requests avoid a redundant schema-disclosure model round."""
from unittest import mock

from app.agent.agent_loop import run_agent, run_agent_stream
from app.agent.llm_client import LLMResult
from app.agent.tools import USER_CATALOG, TOOLS


def test_common_gpu_query_executes_on_first_nonstream_round():
    llm = mock.Mock()
    llm.complete.side_effect = [
        LLMResult(tool_calls=[{"id": "t1", "name": "get_gpu_status", "input": {}}]),
        LLMResult(text="GPU 空闲"),
    ]
    execute = mock.Mock(return_value=(True, "GPU 空闲"))
    out = run_agent(llm, "system", [{"role": "user", "content": "查看 GPU 状态"}],
                    USER_CATALOG, execute)
    assert out["reply"] == "GPU 空闲"
    assert llm.complete.call_count == 2
    execute.assert_called_once_with("get_gpu_status", {})
    first_tools = {tool["name"]: tool for tool in llm.complete.call_args_list[0].args[2]}
    assert first_tools["get_gpu_status"] == next(
        tool for tool in TOOLS if tool["name"] == "get_gpu_status")


def test_common_gpu_query_executes_on_first_stream_round():
    llm = mock.Mock()
    llm.stream_complete.side_effect = [
        iter([{"type": "tool_use", "id": "t1", "name": "get_gpu_status", "input": {}}]),
        iter([{"type": "text", "delta": "GPU 空闲"}]),
    ]
    execute = mock.Mock(return_value=(True, "GPU 空闲"))
    events = list(run_agent_stream(
        llm, "system", [{"role": "user", "content": "查看 GPU 状态"}],
        USER_CATALOG, execute))
    assert [event["event"] for event in events] == ["tool_use", "tool_result", "text", "done"]
    assert llm.stream_complete.call_count == 2
    execute.assert_called_once_with("get_gpu_status", {})


def test_unexpected_tool_still_uses_disclosure_before_execution():
    llm = mock.Mock()
    llm.complete.side_effect = [
        LLMResult(tool_calls=[{"id": "t1", "name": "get_gpu_status", "input": {}}]),
        LLMResult(tool_calls=[{"id": "t2", "name": "get_gpu_status", "input": {}}]),
        LLMResult(text="GPU 空闲"),
    ]
    execute = mock.Mock(return_value=(True, "GPU 空闲"))
    out = run_agent(llm, "system", [{"role": "user", "content": "你好"}],
                    USER_CATALOG, execute)
    assert out["reply"] == "GPU 空闲"
    assert llm.complete.call_count == 3
    execute.assert_called_once_with("get_gpu_status", {})
