"""Read-only + protection tools: ownership enforced, live queries, structured output."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import tools as agent_tools


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _mk_user(db, name="owen", role="user"):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_inst(db, user, status="running", protected=False, container_id=None):
    inst = models.ContainerInstance(
        user_id=user.id,
        container_id=container_id or ("a" * 64),
        image="basic:v1", gpu_ids=[0], gpu_count=1,
        status=status, cleanup_protected=protected,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def test_list_containers_only_own(monkeypatch, db):
    u = _mk_user(db)
    _mk_inst(db, u)
    other = _mk_user(db, "pat")
    _mk_inst(db, other, container_id="b" * 64)
    # docker_runner imported at module scope; point it at a stub
    stub = type("DockerStub", (), {"is_container_running": lambda self, cid: True})()
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("list_containers", {})
    assert ok is True
    assert text.count("a" * 12) >= 1 and text.count("b" * 12) == 0


def test_get_status_denies_other_owner(monkeypatch, db):
    u = _mk_user(db)
    other = _mk_user(db, "quin")
    inst = _mk_inst(db, other, container_id="c" * 64)
    stub = type("DockerStub", (), {"is_container_running": lambda self, cid: True})()
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("get_container_status", {"id": inst.id})
    assert ok is False
    assert "not authorized" in text.lower() or "未授权" in text


def test_set_protection_persists(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, protected=False)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("set_container_protection", {"id": inst.id, "protected": True})
    assert ok is True
    db.refresh(inst)
    assert inst.cleanup_protected is True
