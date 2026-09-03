"""agent_loop: ReAct tool loop, ≤5 calls, returns reply + tool_trace."""
from unittest import mock

from app.agent.agent_loop import run_agent
from app.agent.llm_client import LLMResult


def _llm_with(script):
    """script: list of (llm_result, executor_result) rounds, then final LLMResult."""
    llm = mock.Mock()

    def side_effect(*a, **kw):
        if not hasattr(side_effect, "i"):
            side_effect.i = 0
        idx = side_effect.i
        side_effect.i += 1
        return script[idx]

    llm.complete.side_effect = side_effect
    return llm


def test_single_tool_then_reply():
    llm = _llm_with([
        LLMResult(tool_calls=[{"id": "a", "name": "get_gpu_status", "input": {}}]),
        LLMResult(text="当前有 4 张卡，均空闲"),
    ])
    executed = []
    executor = lambda name, inp: executed.append((name, inp)) or (True, "4 GPUs idle")

    out = run_agent(llm, "sys", [], [], executor, max_calls=5)

    assert out["reply"] == "当前有 4 张卡，均空闲"
    assert executed == [("get_gpu_status", {})]
    assert out["tool_trace"] == [{"tool": "get_gpu_status", "ok": True, "result": "4 GPUs idle"}]


def test_plain_reply_no_tools():
    llm = _llm_with([LLMResult(text="这是状态摘要")])
    out = run_agent(llm, "sys", [], [], lambda n, i: (False, "unused"), max_calls=5)
    assert out["reply"] == "这是状态摘要"
    assert out["tool_trace"] == []


def test_max_calls_bounded():
    # Always emits a tool call → loop must stop after max_calls and not hang.
    llm = _llm_with([LLMResult(tool_calls=[{"id": str(i), "name": "list_containers", "input": {}}]) for i in range(10)])
    out = run_agent(llm, "sys", [], [], lambda n, i: (True, "ok"), max_calls=3)
    assert len(out["tool_trace"]) == 3
    assert llm.complete.call_count <= 4  # 3 tools + final attempt
