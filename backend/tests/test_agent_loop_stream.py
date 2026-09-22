"""run_agent_stream emits text/tool_use/tool_result/done events in order."""
from unittest import mock

from app.agent.agent_loop import run_agent_stream


def _scripted_llm(rounds, final_text="容器已停止"):
    """rounds: list of list-of-events per LLM call. Last call = final text reply."""
    llm = mock.Mock()
    calls = rounds + [iter([
        {"type": "text", "delta": final_text[:2]},
        {"type": "text", "delta": final_text[2:]},
    ])]
    it = iter(calls)
    llm.stream_complete.side_effect = lambda *a, **kw: next(it)
    return llm


def test_stream_tool_then_final_text():
    llm = _scripted_llm([
        iter([
            {"type": "text", "delta": "好的"},
            {"type": "tool_use", "id": "t1", "name": "stop_container", "input": {"id": 3}},
        ]),
    ])
    executed = []
    executor = lambda n, i: executed.append((n, i)) or (True, "stopped")

    events = list(run_agent_stream(llm, "sys", [], [], executor, max_calls=5))

    kinds = [e["event"] for e in events]
    assert kinds == ["text", "tool_use", "tool_result", "text", "text", "done"]
    assert executed == [("stop_container", {"id": 3})]
    done = events[-1]
    assert done["reply"] == "容器已停止"
    assert done["tool_trace"] == [{"tool": "stop_container", "ok": True, "result": "stopped"}]


def test_stream_plain_reply_no_tools():
    llm = mock.Mock()
    llm.stream_complete.side_effect = lambda *a, **kw: iter([
        {"type": "text", "delta": "状态正常"},
    ])
    events = list(run_agent_stream(llm, "sys", [], [], lambda n, i: (False, "x"), max_calls=3))
    assert [e["event"] for e in events] == ["text", "done"]
    assert events[-1]["tool_trace"] == []


def test_stream_create_validation_failure_finishes_without_another_llm_round():
    llm = mock.Mock()
    llm.stream_complete.return_value = iter([
        {"type": "tool_use", "id": "t1", "name": "create_container", "input": {
            "image_id": 1, "gpu_count": 1}},
    ])
    failure = "创建配置校验失败：用户要求 0 张 GPU；未创建容器。"

    events = list(run_agent_stream(llm, "sys", [], [],
                                   lambda *_: (False, failure), max_calls=5))

    assert [event["event"] for event in events] == ["tool_use", "tool_result", "done"]
    assert events[-1]["reply"] == failure
    llm.stream_complete.assert_called_once()


def test_stream_start_claim_without_tool_is_not_displayed_as_success():
    llm = mock.Mock()
    llm.stream_complete.side_effect = lambda *_args: iter([
        {"type": "text", "delta": "容器已启动，正在运行中"},
    ])
    events = list(run_agent_stream(
        llm, "sys", [{"role": "user", "content": "启动ollama容器"}], [],
        lambda *_: (True, "unused"), max_calls=5))
    assert [event["event"] for event in events] == ["done"]
    assert "没有执行启动容器操作" in events[-1]["reply"]
    assert llm.stream_complete.call_count == 2
