"""Cancel support at the loop level: cancel_check stops before the next external
effect (LLM round or tool execution) and returns INTERRUPT_REPLY ("请求中断").

Plain-list tools are used (not CATALOG) so disclosure intercepts cannot mask
"cancel before tool execution" — an active tool must not run after cancel.
"""
from unittest import mock

from app.agent.agent_loop import run_agent, run_agent_stream, INTERRUPT_REPLY
from app.agent.llm_client import LLMResult

TOOLS = [{"name": "list_containers", "description": "list", "input_schema": {}}]


# ── run_agent (sync) ─────────────────────────────────────────────────────────

def test_run_agent_cancelled_before_first_llm():
    llm = mock.Mock()
    llm.complete.side_effect = AssertionError("canceled before any LLM round")
    # cancel_check 恒真：第一轮循环在碰 LLM 之前就停。
    out = run_agent(llm, "sys", [], TOOLS, lambda n, i: (True, "ok"),
                    max_calls=3, cancel_check=lambda: True)
    assert out == {"reply": INTERRUPT_REPLY, "tool_trace": []}
    llm.complete.assert_not_called()


def test_run_agent_cancelled_mid_round_stops_before_tool():
    """取消在 LLM 调用期间到达 → 返回工具调用后，执行器一次都不该被碰。"""
    state = {"cancelled": False}

    def complete(*a, **kw):
        state["cancelled"] = True  # 取消发生在这轮 LLM 往返内
        return LLMResult(tool_calls=[{"id": "x", "name": "list_containers", "input": {}}])

    llm = mock.Mock()
    llm.complete.side_effect = complete
    executed = []
    out = run_agent(llm, "sys", [], TOOLS,
                    lambda n, i: executed.append((n, i)) or (True, "ok"),
                    max_calls=3, cancel_check=lambda: state["cancelled"])
    assert out["reply"] == INTERRUPT_REPLY
    assert executed == []  # 工具未执行
    assert out["tool_trace"] == []
    assert llm.complete.call_count == 1


def test_run_agent_cancelled_during_final_reply_generation():
    """非流式：取消在阻塞式回复生成期间到达 → 已生成的答案也不返回，按中断处理。"""
    state = {"cancelled": False}

    def complete(*a, **kw):
        state["cancelled"] = True  # 取消发生在这次 complete 等待期间
        return LLMResult(text="这条完整回答不应被返回")

    llm = mock.Mock()
    llm.complete.side_effect = complete
    out = run_agent(llm, "sys", [], TOOLS, lambda n, i: (True, "ok"),
                    max_calls=3, cancel_check=lambda: state["cancelled"])
    assert out["reply"] == INTERRUPT_REPLY
    assert llm.complete.call_count == 1


def test_run_agent_cancel_check_defaults_to_never():
    """不传 cancel_check 时行为与旧版完全一致（回归护栏）。"""
    llm = mock.Mock()
    llm.complete.side_effect = [
        LLMResult(tool_calls=[{"id": "a", "name": "list_containers", "input": {}}]),
        LLMResult(text="done"),
    ]
    out = run_agent(llm, "sys", [], TOOLS, lambda n, i: (True, "ok"), max_calls=3)
    assert out["reply"] == "done"
    assert len(out["tool_trace"]) == 1


# ── run_agent_stream ─────────────────────────────────────────────────────────

def test_run_agent_stream_cancelled_before_first_round():
    llm = mock.Mock()
    llm.stream_complete.side_effect = AssertionError("canceled before any round")
    events = list(run_agent_stream(llm, "sys", [], TOOLS, lambda n, i: (True, "ok"),
                                   max_calls=3, cancel_check=lambda: True))
    assert events == [{"event": "done", "reply": INTERRUPT_REPLY, "tool_trace": []}]
    llm.stream_complete.assert_not_called()


def test_run_agent_stream_cancelled_mid_reply_stops_text():
    """核心：正在逐字回复时点停止 → 已发出的增量保留，后续增量不再产出，
    直接 done(请求中断) 收尾（此前这段没有轮询点，停止会失效直到整段答完）。"""
    state = {"cancelled": False}

    def events():
        yield {"type": "text", "delta": "前两字"}      # cancel 尚未置位 → 发出
        state["cancelled"] = True                    # 模型产出下一条前，用户点停止
        yield {"type": "text", "delta": "后续长文不应继续出现"}

    llm = mock.Mock()
    llm.stream_complete.side_effect = lambda *a, **k: events()
    executed = []
    evs = list(run_agent_stream(llm, "sys", [], TOOLS,
                                lambda n, i: executed.append((n, i)) or (True, "ok"),
                                max_calls=3, cancel_check=lambda: state["cancelled"]))
    assert [e["event"] for e in evs] == ["text", "done"]
    assert evs[0]["delta"] == "前两字"
    assert evs[-1]["reply"] == INTERRUPT_REPLY
    assert executed == []          # 未发生工具调用
    assert llm.stream_complete.call_count == 1  # 取消后不进下一轮


def test_run_agent_stream_cancelled_mid_round_stops_before_tool():
    state = {"cancelled": False}

    def sc(*a, **kw):
        state["cancelled"] = True  # 取消发生在这轮 LLM 流式往返内
        return iter([{"type": "tool_use", "id": "x", "name": "list_containers", "input": {}}])

    llm = mock.Mock()
    llm.stream_complete.side_effect = sc
    executed = []
    events = list(run_agent_stream(llm, "sys", [], TOOLS,
                                   lambda n, i: executed.append((n, i)) or (True, "ok"),
                                   max_calls=3, cancel_check=lambda: state["cancelled"]))
    # 只出 done(请求中断)，没有 tool_use/tool_result，执行器未碰，也不再进下一轮。
    assert events == [{"event": "done", "reply": INTERRUPT_REPLY, "tool_trace": []}]
    assert executed == []
    assert llm.stream_complete.call_count == 1
