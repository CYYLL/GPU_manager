"""Stream route emits SSE events; assistant reply persisted after stream."""
import json
from unittest import mock
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, SessionLocal
from app import models
import app.routers.agent as agent_router
from app.agent import tools as agent_tools
from app.agent.llm_client import LLMResult
from app.agent.agent_loop import INTERRUPT_REPLY


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _mk_user(db):
    u = models.User(username="yuki", hashed_password="x", role="user", gpu_quota=8, mode="llm")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_stream_route_requires_llm_mode():
    # traditional user must be rejected before any streaming
    from sqlalchemy.orm import sessionmaker as sm
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    S = sm(autocommit=False, autoflush=False, bind=eng)
    db = S()
    u = models.User(username="zed", hashed_password="x", role="user", gpu_quota=8, mode="traditional")
    db.add(u); db.commit(); db.refresh(u)
    with pytest.raises(HTTPException):
        agent_router.chat_stream(agent_router.ChatRequest(message="hi"), u, db)
    db.close()


def test_stream_route_emits_events_and_persists(monkeypatch, engine):
    # patch SessionLocal to a per-test session factory backed by this engine
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(agent_router, "SessionLocal", TestSess)

    db = TestSess()
    u = _mk_user(db)

    # progressive disclosure: stub round 1 is intercepted (emits nothing),
    # round 2 (schema loaded) actually executes, round 3 gives the reply.
    llm = mock.Mock()
    llm.stream_complete.side_effect = [
        iter([{"type": "text", "delta": "好的"}, {"type": "tool_use", "id": "t1",
              "name": "get_gpu_status", "input": {}}]),
        iter([{"type": "tool_use", "id": "t2", "name": "get_gpu_status", "input": {}}]),
        iter([{"type": "text", "delta": "4 张卡空闲"}]),
    ]
    monkeypatch.setattr(agent_router, "llm_client", llm)

    resp = agent_router.chat_stream(agent_router.ChatRequest(message="查 GPU"), u, db)
    # StreamingResponse.body_iterator is async — collect via asyncio.run
    import asyncio

    async def _collect(resp):
        return b"".join([chunk async for chunk in resp.body_iterator])

    raw = asyncio.run(_collect(resp))
    lines = [ln for ln in raw.decode().splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]

    kinds = [e["event"] for e in events]
    assert kinds == ["text", "tool_use", "tool_result", "text", "done"]
    assert events[-1]["event"] == "done"
    assert events[-1]["reply"] == "4 张卡空闲"
    # assistant message persisted in a fresh session
    db2 = TestSess()
    msgs = db2.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).all()
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[-1].content == "4 张卡空闲"
    db2.close()
    db.close()


def test_stream_route_fallback_on_llm_failure(monkeypatch, engine):
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(agent_router, "SessionLocal", TestSess)

    db = TestSess()
    u = _mk_user(db)

    llm = mock.Mock()
    llm.stream_complete.side_effect = Exception("LLM API down")
    monkeypatch.setattr(agent_router, "llm_client", llm)

    resp = agent_router.chat_stream(agent_router.ChatRequest(message="hi"), u, db)
    import asyncio

    async def _collect(resp):
        return b"".join([chunk async for chunk in resp.body_iterator])

    raw = asyncio.run(_collect(resp))
    lines = [ln for ln in raw.decode().splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]

    assert events[-1]["event"] == "done"
    assert "失败" in events[-1]["reply"]
    # role alternation preserved: assistant fallback persisted after the user turn
    db2 = TestSess()
    msgs = db2.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).all()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert "失败" in msgs[-1].content
    db2.close()
    db.close()


def _collect(resp):
    import asyncio

    async def _run(r):
        return b"".join([chunk async for chunk in r.body_iterator])

    return asyncio.run(_run(resp))


def _drain_user_runs(user_id):
    """Test hygiene: clear any leaked run gate for a user (idempotent)."""
    ev = agent_router._RunGate._runs.pop(user_id, None)
    if ev is not None:
        ev.set()


def test_stream_route_cancel_before_stream_returns_interrupt(monkeypatch, engine):
    """停止在流体真正执行前发生 → done('请求中断') 落库，LLM 一次都不该被调用。"""
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(agent_router, "SessionLocal", TestSess)

    db = TestSess()
    u = _mk_user(db)
    try:
        llm = mock.Mock()
        llm.stream_complete.side_effect = [iter([{"type": "text", "delta": "不应出现"}])]
        monkeypatch.setattr(agent_router, "llm_client", llm)

        resp = agent_router.chat_stream(agent_router.ChatRequest(message="帮我查 GPU"), u, db)
        # 用户在流体跑起来前点了停止 → /api/agent/cancel（幂等 200）
        assert agent_router.cancel_chat(u, db)["status"] == "cancelled"
        # 取消后流仍被读完：收到正常的 done(请求中断)，而不是连接中断
        raw = _collect(resp)
        events = [json.loads(ln[6:]) for ln in raw.decode().splitlines() if ln.startswith("data: ")]
        assert [e["event"] for e in events] == ["done"]
        assert events[-1]["reply"] == INTERRUPT_REPLY
        llm.stream_complete.assert_not_called()  # 中断发生在任何 LLM 往返之前
        # 收尾文案已落库，角色交替保持
        db2 = TestSess()
        msgs = db2.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).all()
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[-1].content == INTERRUPT_REPLY
        db2.close()
    finally:
        _drain_user_runs(u.id)
        db.close()


def test_stream_route_cancel_mid_stream_stops_before_tool(monkeypatch, engine):
    """模型轮次返回了工具调用、取消恰在此时到达 → 不再执行工具/进入下一轮，直接 done(请求中断)。

    （第一轮的工具本会走"渐进式披露"拦截不执行；真正要区分的是：取消后循环必须
    在此停住 —— 不再有第二轮 LLM 往返、不产生任何 tool_use/tool_result 事件。）
    """
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(agent_router, "SessionLocal", TestSess)

    db = TestSess()
    u = _mk_user(db)
    try:

        def sc(*a, **kw):
            # 在流式往返的"中途"（迭代器取出第一条时）触发取消：模拟用户在模型
            # 已产出工具调用、引擎正要进入工具执行前的窗口点了停止。
            def gen():
                agent_router.cancel_chat(u, db)  # 置位该用户真实 gate
                yield {"type": "tool_use", "id": "c1", "name": "stop_container", "input": {}}
            return gen()

        llm = mock.Mock()
        llm.stream_complete.side_effect = sc
        monkeypatch.setattr(agent_router, "llm_client", llm)

        resp = agent_router.chat_stream(agent_router.ChatRequest(message="停掉那个容器"), u, db)
        events = [json.loads(ln[6:]) for ln in _collect(resp).decode().splitlines()
                  if ln.startswith("data: ")]
        assert [e["event"] for e in events] == ["done"]
        assert events[-1]["reply"] == INTERRUPT_REPLY
        # 没有 tool_use / tool_result 事件，且 LLM 只被调用了一次（取消后不再进下一轮）
        assert llm.stream_complete.call_count == 1
        # 收尾文案落库
        db2 = TestSess()
        msgs = db2.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).all()
        assert msgs[-1].content == INTERRUPT_REPLY
        db2.close()
    finally:
        _drain_user_runs(u.id)
        db.close()


def test_stream_route_cancel_mid_reply_stops_text_and_persists(monkeypatch, engine):
    """回复正在流式产出时停止 → 已发增量保留、后续不再产出，落库 请求中断。

    回归验证用户报告的核心场景：此前回复段没有轮询点，点停止要等整段答完才生效。
    """
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(agent_router, "SessionLocal", TestSess)

    db = TestSess()
    u = _mk_user(db)
    try:
        state = {"cancelled": False}

        def events():
            yield {"type": "text", "delta": "已开始"}   # 先发出两个增量…
            yield {"type": "text", "delta": "回答"}
            agent_router.cancel_chat(u, db)          # …下一条增量前用户点停止
            yield {"type": "text", "delta": "这一整段不该出现"}

        llm = mock.Mock()
        llm.stream_complete.side_effect = lambda *a, **k: events()
        monkeypatch.setattr(agent_router, "llm_client", llm)

        resp = agent_router.chat_stream(agent_router.ChatRequest(message="写一段分析"), u, db)
        events_list = [json.loads(ln[6:]) for ln in _collect(resp).decode().splitlines()
                       if ln.startswith("data: ")]
        assert [e["event"] for e in events_list] == ["text", "text", "done"]
        deltas = [e["delta"] for e in events_list if e["event"] == "text"]
        assert deltas == ["已开始", "回答"]
        assert events_list[-1]["reply"] == INTERRUPT_REPLY
        assert llm.stream_complete.call_count == 1
        db2 = TestSess()
        msgs = db2.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).all()
        assert msgs[-1].content == INTERRUPT_REPLY  # 用户停止，历史记录为 请求中断
        db2.close()
    finally:
        _drain_user_runs(u.id)
        db.close()


def test_stream_route_single_flight_409_then_releases(monkeypatch, engine):
    """同用户并发第二条流被 409；第一条流读完释放 gate 后可再次发起。"""
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(agent_router, "SessionLocal", TestSess)

    db = TestSess()
    u = _mk_user(db)
    try:
        llm = mock.Mock()
        llm.stream_complete.side_effect = [
            iter([{"type": "text", "delta": "第一次回复"}]),
            iter([{"type": "text", "delta": "释放后的回复"}]),
        ]
        monkeypatch.setattr(agent_router, "llm_client", llm)

        resp1 = agent_router.chat_stream(agent_router.ChatRequest(message="第一次"), u, db)
        with pytest.raises(HTTPException) as ei:
            agent_router.chat_stream(agent_router.ChatRequest(message="并发第二条"), u, db)
        assert ei.value.status_code == 409

        # 第一条流读完 → 释放 gate → 第三条能拿到 200（不再 409）
        raw1 = _collect(resp1)
        assert "第一次回复" in raw1.decode()
        resp2 = agent_router.chat_stream(agent_router.ChatRequest(message="第三次"), u, db)
        raw2 = _collect(resp2)
        assert "释放后的回复" in raw2.decode()
    finally:
        _drain_user_runs(u.id)
        db.close()


def test_cancel_chat_idempotent_no_running(engine):
    """没有进行中请求时 cancel 也是 200（幂等），前端无需区分。"""
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestSess()
    u = _mk_user(db)
    try:
        assert agent_router.cancel_chat(u, db)["status"] == "cancelled"
    finally:
        _drain_user_runs(u.id)
        db.close()


def test_agent_status_reflects_run_gate(engine):
    """/api/agent/status：gate 未持有 → false；持有中 → true；释放后再 → false。"""
    from sqlalchemy.orm import sessionmaker
    TestSess = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestSess()
    u = _mk_user(db)
    try:
        # 闲置：没有进行中的请求
        assert agent_router.agent_status(u) == {"running": False}

        # 模拟一条请求正在跑：acquire 后 gate 被持有
        gate = agent_router._RunGate.acquire(u.id)
        assert gate is not None
        assert agent_router.agent_status(u) == {"running": True}

        # 释放后回到闲置（对账轮询正是等这个翻转）
        agent_router._RunGate.release(u.id, gate)
        assert agent_router.agent_status(u) == {"running": False}
    finally:
        _drain_user_runs(u.id)
        db.close()


def test_agent_status_requires_llm_mode():
    """传统模式用户访问 status → 403（与其它 agent 路由一致）。"""
    from sqlalchemy.orm import sessionmaker as sm
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    S = sm(autocommit=False, autoflush=False, bind=eng)
    db = S()
    u = models.User(username="ned", hashed_password="x", role="user",
                    gpu_quota=8, mode="traditional")
    db.add(u)
    db.commit()
    db.refresh(u)
    try:
        with pytest.raises(HTTPException) as ei:
            agent_router.agent_status(u)
        assert ei.value.status_code == 403
    finally:
        db.close()
