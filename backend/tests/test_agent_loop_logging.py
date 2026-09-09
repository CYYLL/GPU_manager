"""Run-health logging in agent_loop: empty model replies must surface as a
WARNING with an EMPTY marker; normal replies log INFO. Guards the fix so a
"model returned nothing" incident is visible in journal without DB forensics."""
import logging
from unittest import mock

from app.agent.agent_loop import (run_agent, run_agent_stream,
                                  EMPTY_REPLY_FALLBACK, TOOL_CALL_LIMIT_FALLBACK)
from app.agent.llm_client import LLMResult

_LOGGER = "app.agent.agent_loop"


def _messages(caplog, level=logging.WARNING):
    return [(r.levelno, r.message)
            for r in caplog.records
            if r.name == _LOGGER and r.levelno >= level]


def _allow_propagation(monkeypatch):
    # Some route tests import app.main, whose startup config gives the "app"
    # logger a real handler and propagate=False — that would swallow records
    # before caplog's root handler sees them. Force propagation for capture.
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)


def test_empty_reply_logs_warning_sync(caplog, monkeypatch):
    _allow_propagation(monkeypatch)
    caplog.set_level(logging.INFO, logger=_LOGGER)
    llm = mock.Mock()
    llm.complete.return_value = LLMResult(text="", tool_calls=[])
    # 预算未耗尽即空回复 → EMPTY fallback（区别于预算耗尽的 LIMIT fallback）
    out = run_agent(llm, "sys", [], [], lambda n, i: (True, "ok"), max_calls=5)
    assert out["reply"] == EMPTY_REPLY_FALLBACK
    warns = [m for lvl, m in _messages(caplog) if lvl == logging.WARNING]
    assert any("EMPTY" in m for m in warns), warns


def test_sync_budget_exhausted_empty_uses_limit_fallback():
    # 每轮都调工具、占满 max_calls，强制收尾轮仍空 → 真·达到调用上限文案
    llm = mock.Mock()
    llm.complete.side_effect = (
        [LLMResult(tool_calls=[{"id": "a", "name": "get_gpu_status", "input": {}}])
         for _ in range(5)]
        + [LLMResult(text="", tool_calls=[])]  # forced no-tool final → empty
    )
    out = run_agent(llm, "sys", [], [], lambda n, i: (True, "ok"), max_calls=5)
    assert out["reply"] == TOOL_CALL_LIMIT_FALLBACK
    assert out["tool_trace"] == [{"tool": "get_gpu_status", "ok": True, "result": "ok"}] * 5


def test_stream_empty_reply_falls_back_and_warns(caplog, monkeypatch):
    _allow_propagation(monkeypatch)
    caplog.set_level(logging.INFO, logger=_LOGGER)
    llm = mock.Mock()
    llm.stream_complete.return_value = iter([])  # no text deltas, no tool_use
    # 第 0 轮（round_idx < max_calls）空手而归 → EMPTY fallback
    events = list(run_agent_stream(llm, "sys", [], [], lambda n, i: (True, "ok"),
                                   max_calls=5))
    assert events[-1] == {"event": "done", "reply": EMPTY_REPLY_FALLBACK,
                          "tool_trace": []}
    warns = [m for lvl, m in _messages(caplog) if lvl == logging.WARNING]
    assert any("EMPTY" in m for m in warns), warns


def test_stream_budget_exhausted_empty_uses_limit_fallback():
    # 两轮都调工具占满 max_calls，第 3 轮（round_idx == max_calls）强制收尾仍空
    # → 真·达到调用上限文案（区别于预算未耗尽的 EMPTY fallback）
    llm = mock.Mock()
    rounds = iter([
        iter([{"type": "tool_use", "id": "t1", "name": "get_gpu_status", "input": {}}]),
        iter([{"type": "tool_use", "id": "t2", "name": "get_gpu_status", "input": {}}]),
        iter([]),  # forced no-tool final round at round_idx == max_calls
    ])
    llm.stream_complete.side_effect = lambda *a, **k: next(rounds)
    events = list(run_agent_stream(llm, "sys", [], [], lambda n, i: (True, "ok"),
                                   max_calls=2))
    assert events[-1] == {"event": "done", "reply": TOOL_CALL_LIMIT_FALLBACK,
                          "tool_trace": [{"tool": "get_gpu_status", "ok": True,
                                          "result": "ok"}] * 2}


def test_tool_round_and_done_logged_info(caplog, monkeypatch):
    _allow_propagation(monkeypatch)
    caplog.set_level(logging.INFO, logger=_LOGGER)
    llm = mock.Mock()
    llm.complete.side_effect = [
        LLMResult(tool_calls=[{"id": "a", "name": "get_gpu_status", "input": {}}]),
        LLMResult(text="当前有 4 张卡，均空闲"),
    ]
    out = run_agent(llm, "sys", [], [],
                    lambda n, i: (True, "4 GPUs idle"), max_calls=5)
    assert out["reply"] == "当前有 4 张卡，均空闲"
    infos = [m for lvl, m in _messages(caplog, logging.INFO)]
    assert any("round=1/5 tool_calls=1" in m for m in infos), infos
    assert any("agent tool ok=True name=get_gpu_status" in m for m in infos), infos
    assert any("agent done reply_len=" in m for m in infos), infos
