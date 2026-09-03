"""Chat route: requires llm mode, persists messages, returns agent reply."""
from unittest import mock
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import models

# isolate DB + auth via TestClient dependency override is heavy; test handler
# functions directly (mirrors test_mode.py pattern) with a mocked llm.
import app.routers.agent as agent_router
from app.agent.llm_client import LLMResult


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _mk_user(db, mode="llm"):
    u = models.User(username="ruby", hashed_password="x", role="user", gpu_quota=8, mode=mode)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_chat_requires_llm_mode(db):
    u = _mk_user(db, mode="traditional")
    with pytest.raises(Exception):
        agent_router.chat(agent_router.ChatRequest(message="hi"), u, db)  # require_llm_mode raises 403


def test_chat_returns_reply_and_persists(monkeypatch, db):
    u = _mk_user(db)
    llm = mock.Mock()
    llm.complete.return_value = LLMResult(text="你有 1 个容器在运行")
    monkeypatch.setattr(agent_router, "llm_client", llm)
    monkeypatch.setattr(agent_router, "ToolExecutor",
                        lambda db, user: mock.Mock(run=lambda n, i: (True, "ok")))

    out = agent_router.chat(agent_router.ChatRequest(message="查一下我的容器"), u, db)

    assert out["reply"] == "你有 1 个容器在运行"
    msgs = db.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).all()
    assert len(msgs) == 2  # user + assistant


def test_chat_llm_failure_persists_fallback(monkeypatch, db):
    u = _mk_user(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(agent_router, "run_agent", _boom)

    out = agent_router.chat(agent_router.ChatRequest(message="hi"), u, db)

    # no 500: fallback assistant reply returned
    assert out["reply"] == "模型调用失败，请稍后重试"
    assert out["tool_trace"] == []
    # history alternates: user then assistant fallback
    msgs = db.query(models.ChatMessage).filter(
        models.ChatMessage.user_id == u.id
    ).order_by(models.ChatMessage.id.asc()).all()
    assert [m.role for m in msgs] == ["user", "assistant"]


def test_session_history_and_clear(db):
    u = _mk_user(db)
    db.add(models.ChatMessage(user_id=u.id, role="user", content="hello"))
    db.commit()
    history = agent_router.get_session(u, db)
    assert any(m["role"] == "user" and m["content"] == "hello" for m in history["messages"])
    agent_router.clear_session(u, db)
    assert db.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).count() == 0
