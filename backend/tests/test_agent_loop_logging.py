"""Run-health logging in agent_loop: empty model replies must surface as a
WARNING with an EMPTY marker; normal replies log INFO. Guards the fix so a
"model returned nothing" incident is visible in journal without DB forensics."""
import logging
from unittest import mock

from app.agent.agent_loop import (run_agent, run_agent_stream,
                                  EMPTY_REPLY_FALLBACK)
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
    out = run_agent(llm, "sys", [], [], lambda n, i: (True, "ok"), max_calls=5)
    assert out["reply"] == ""  # existing sync behavior: returns empty as-is
    warns = [m for lvl, m in _messages(caplog) if lvl == logging.WARNING]
    assert any("EMPTY" in m for m in warns), warns


def test_stream_empty_reply_falls_back_and_warns(caplog, monkeypatch):
    _allow_propagation(monkeypatch)
    caplog.set_level(logging.INFO, logger=_LOGGER)
    llm = mock.Mock()
    llm.stream_complete.return_value = iter([])  # no text deltas, no tool_use
    events = list(run_agent_stream(llm, "sys", [], [], lambda n, i: (True, "ok"),
                                   max_calls=5))
    assert events[-1] == {"event": "done", "reply": EMPTY_REPLY_FALLBACK,
                          "tool_trace": []}
    warns = [m for lvl, m in _messages(caplog) if lvl == logging.WARNING]
    assert any("EMPTY" in m for m in warns), warns


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
