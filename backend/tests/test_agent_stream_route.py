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

    llm = mock.Mock()
    llm.stream_complete.side_effect = [
        iter([{"type": "text", "delta": "好的"}, {"type": "tool_use", "id": "t1",
              "name": "get_gpu_status", "input": {}}]),
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
