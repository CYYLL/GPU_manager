"""End-to-end: a chat message asking to stop a container actually stops it."""
from unittest import mock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
import app.routers.containers as containers_router
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


def test_chat_stop_container_flow(monkeypatch, db):
    u = models.User(username="xena", hashed_password="x", role="user", gpu_quota=8, mode="llm")
    db.add(u)
    db.commit()
    db.refresh(u)
    inst = models.ContainerInstance(
        user_id=u.id, container_id="6" * 64, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status="running",
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)

    # docker_runner mock (router module global): stop succeeds
    docker_stub = mock.Mock()
    docker_stub.stop_container.return_value = (True, "stopped")
    docker_stub.is_container_running.return_value = False
    monkeypatch.setattr(containers_router, "docker_runner", docker_stub)

    # scripted LLM: first call asks to stop, second call gives the reply
    llm = mock.Mock()
    llm.complete.side_effect = [
        LLMResult(tool_calls=[{"id": "t1", "name": "stop_container", "input": {"id": inst.id}}]),
        LLMResult(text="好的，容器已停止"),
    ]
    monkeypatch.setattr(agent_router, "llm_client", llm)

    out = agent_router.chat(agent_router.ChatRequest(message=f"帮我停止容器 {inst.id}"), u, db)

    assert out["reply"] == "好的，容器已停止"
    assert out["tool_trace"][0]["tool"] == "stop_container"
    assert out["tool_trace"][0]["ok"] is True
    docker_stub.stop_container.assert_called_once()
    db.refresh(inst)
    assert inst.status == "stopped"
    # event recorded as llm source
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.event == "stop" and ev.source == "llm"
