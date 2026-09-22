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


def test_chat_rejects_explicit_zero_gpu_without_calling_llm(monkeypatch, db):
    u = _mk_user(db)
    db.add(models.GpuImage(name="Ollama", image="ollama/ollama:latest", min_gpu=1))
    db.commit()
    llm = mock.Mock()
    monkeypatch.setattr(agent_router, "llm_client", llm)

    out = agent_router.chat(agent_router.ChatRequest(
        message="帮我根据ollama/ollama:latest创建一个GPU设定为0的容器"), u, db)

    assert "最低 GPU 数为 1" in out["reply"]
    assert "未创建容器，也未分配 GPU" in out["reply"]
    assert out["tool_trace"] == []
    llm.complete.assert_not_called()


def test_chat_rejects_chinese_zero_gpu_without_calling_llm(monkeypatch, db):
    u = _mk_user(db)
    llm = mock.Mock()
    monkeypatch.setattr(agent_router, "llm_client", llm)

    out = agent_router.chat(agent_router.ChatRequest(
        message="创建GPU设定为零的容器"), u, db)

    assert "未创建容器" in out["reply"]
    llm.complete.assert_not_called()


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


def test_tool_calls_survive_session_reload_without_results(monkeypatch, db):
    u = _mk_user(db)
    monkeypatch.setattr(agent_router, "ToolExecutor", lambda *_: mock.Mock())
    monkeypatch.setattr(agent_router, "run_agent", lambda *_args, **_kwargs: {
        "reply": "查询完成", "tool_trace": [
            {"tool": "list_containers", "ok": True, "result": "private data"},
            {"tool": "inspect_image", "ok": False, "result": "private error"},
        ],
    })

    agent_router.chat(agent_router.ChatRequest(message="查询"), u, db)
    messages = agent_router.get_session(u, db)["messages"]

    assert messages[-1]["tool_calls"] == [
        {"tool": "list_containers", "ok": True},
        {"tool": "inspect_image", "ok": False},
    ]
    assert "private data" not in str(messages)
    assert "private error" not in str(messages)
    assert agent_router._history(db, u.id)[-1] == {"role": "assistant", "content": "查询完成"}


def test_image_pull_guidance_is_role_specific(db):
    user = _mk_user(db)
    user_prompt, user_catalog = agent_router._agent_context(user)
    assert "请联系管理员" in user_prompt
    assert "不要描述管理员的具体操作" in user_prompt
    assert "search_hub_images" not in user_prompt
    assert "pull_hub_image" not in user_prompt
    assert "pull_hub_image" not in user_catalog
    assert "search_hub_images" not in user_catalog
    assert "list_local_images" in user_catalog

    user.role = "admin"
    admin_prompt, admin_catalog = agent_router._agent_context(user)
    assert "当前对话账号是管理员" in admin_prompt
    assert "pull_hub_image" in admin_catalog
