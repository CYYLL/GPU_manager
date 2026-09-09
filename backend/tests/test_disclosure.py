"""Progressive disclosure (stub + sticky activation + auto-correction):
catalog shape + agent_loop interception behavior."""
from unittest import mock

from app.agent import tools as t
from app.agent.agent_loop import run_agent, run_agent_stream
from app.agent.llm_client import LLMResult


def test_tools_export_has_no_extra_keys_and_summaries_cover_all():
    names = {x["name"] for x in t.TOOLS}
    assert {"check_gpu_quota", "stop_container", "create_container",
            "rebuild_container", "get_gpu_status"} <= names
    # TOOLS goes to the gateway verbatim → must never carry non-schema keys (e.g. summary)
    for entry in t.TOOLS:
        assert set(entry) == {"name", "description", "input_schema"}
    assert set(t.TOOL_SUMMARIES) == names
    assert all(v for v in t.TOOL_SUMMARIES.values())


def test_catalog_round_specs_all_stub_then_active_full():
    cat = t.CATALOG
    all_names = {x["name"] for x in t.TOOLS}
    specs = cat.round_specs(set())
    assert {s["name"] for s in specs} == all_names
    for s in specs:  # 未激活 → stub：一句话 + 空 properties，且无多余键
        assert set(s) == {"name", "description", "input_schema"}
        assert s["input_schema"]["properties"] == {}
    specs2 = cat.round_specs({"stop_container"})
    assert len(specs2) == len(t.TOOLS)  # 每轮仍声明全部工具
    full = next(s for s in specs2 if s["name"] == "stop_container")
    assert full["input_schema"]["required"] == ["id"]
    assert set(full) == {"name", "description", "input_schema"}
    assert "input_schema" in cat.activation_text("stop_container")


def test_run_agent_intercepts_stub_then_executes():
    cat = t.CATALOG
    # run_agent 原地改写同一个 working 列表，call_args 只存对象引用 → 在调用时快照。
    results = [
        LLMResult(tool_calls=[{"id": "s1", "name": "stop_container", "input": {}}]),           # stub 轮 → 拦截
        LLMResult(tool_calls=[{"id": "s2", "name": "stop_container", "input": {"id": 9}}]),    # 全量轮 → 执行
        LLMResult(text="好的，容器已停止"),
    ]
    snapshots = []  # (working 副本, round_tools 副本)
    llm = mock.Mock()

    def _capture(system, working, tools):
        snapshots.append((list(working), list(tools)))
        return results.pop(0)

    llm.complete.side_effect = _capture
    executed = []
    out = run_agent(llm, "sys", [], cat,
                    lambda n, i: executed.append((n, i)) or (True, "stopped"),
                    max_calls=5)
    assert executed == [("stop_container", {"id": 9})]   # stub 调用从未到 executor
    assert out["tool_trace"] == [{"tool": "stop_container", "ok": True, "result": "stopped"}]
    assert llm.complete.call_count == 3
    sent_r1 = snapshots[0][1]
    sent_r2 = snapshots[1][1]
    assert all(s["input_schema"]["properties"] == {} for s in sent_r1)   # 第1轮全 stub
    assert any(s["name"] == "stop_container" and s["input_schema"]["required"] == ["id"]
               for s in sent_r2)                                          # 第2轮该工具全量
    working2 = snapshots[1][0]                                            # call#2 时的 working
    assert working2[-2]["role"] == "assistant" and working2[-1]["role"] == "user"
    assert "input_schema" in working2[-1]["content"][0]["content"]        # 拦截轮已回注 schema


def test_run_agent_stream_intercept_round_emits_nothing():
    cat = t.CATALOG
    rounds = iter([
        iter([{"type": "text", "delta": "好的"},
              {"type": "tool_use", "id": "x1", "name": "get_gpu_status", "input": {}}]),      # 拦截，无事件
        iter([{"type": "tool_use", "id": "x2", "name": "get_gpu_status", "input": {}}]),       # 已激活 → 执行
        iter([{"type": "text", "delta": "当前 4 卡空闲"}]),
    ])
    llm = mock.Mock()
    llm.stream_complete.side_effect = lambda *a, **k: next(rounds)
    executed = []
    events = list(run_agent_stream(llm, "sys", [], cat,
                                   lambda n, i: executed.append((n, i)) or (True, "4 idle"),
                                   max_calls=5))
    assert [e["event"] for e in events] == ["text", "tool_use", "tool_result", "text", "done"]
    assert executed == [("get_gpu_status", {})]   # 只在第2轮执行一次
    assert events[-1]["tool_trace"] == [{"tool": "get_gpu_status", "ok": True, "result": "4 idle"}]
