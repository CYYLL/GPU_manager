"""Mutating tools delegate to lifecycle handlers; ownership + errors mapped."""
from unittest import mock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import tools as agent_tools
from app import schemas


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


def _mk_user(db, name="vince"):
    u = models.User(username=name, hashed_password="x", role="user", gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_inst(db, user, status="running", cid="9" * 64):
    inst = models.ContainerInstance(
        user_id=user.id, container_id=cid, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status=status,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def test_stop_container_tool(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    # patch the router module's docker_runner (handlers use it as a module global)
    from app.routers import containers as containers_router
    stub = mock.Mock()
    stub.stop_container.return_value = (True, "stopped")
    stub.is_container_running.return_value = False
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("stop_container", {"id": inst.id})
    assert ok is True
    assert "stopped" in text.lower() or "已" in text
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.source == "llm"


def test_stop_container_denies_other_owner(monkeypatch, db):
    u = _mk_user(db)
    other = _mk_user(db, "wade")
    inst = _mk_inst(db, other, cid="8" * 64)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("stop_container", {"id": inst.id})
    assert ok is False
    assert "not authorized" in text.lower() or "未授权" in text or "not running" in text.lower()


def test_delete_container_tool(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, cid="7" * 64)
    from app.routers import containers as containers_router
    stub = mock.Mock()
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("delete_container", {"id": inst.id})
    assert ok is True


def test_tools_list_has_mutating_schemas():
    names = {t["name"] for t in agent_tools.TOOLS}
    assert {"create_container", "start_container", "stop_container", "delete_container", "list_images"} <= names
    by_name = {t["name"]: t for t in agent_tools.TOOLS}
    assert by_name["stop_container"]["input_schema"]["required"] == ["id"]


def test_create_container_happy_path_returns_password(monkeypatch, db, tmp_path):
    from app.routers import containers as containers_router
    u = _mk_user(db)
    im = models.GpuImage(name="pytorch:latest", image="docker.io/library/pytorch:latest", min_gpu=1)
    db.add(im)
    db.commit()
    db.refresh(im)

    # isolated workspace mount root so the impl never writes to the real /amax tree
    monkeypatch.setenv("CONTAINER_MOUNT_ROOT", str(tmp_path))
    # docker_runner.start_container returns a fake docker id + running status
    stub = mock.Mock()
    stub.start_container.return_value = ("d" * 64, "running")
    monkeypatch.setattr(containers_router, "docker_runner", stub)
    # idle GPUs: availability + busy-GPU checks pass for a single GPU
    monkeypatch.setattr(containers_router.gpu_monitor, "get_gpu_count", lambda: 1)
    monkeypatch.setattr(
        containers_router.gpu_monitor, "get_gpu_status",
        lambda *a, **k: [{"id": 0, "memory_utilization": 0.0, "gpu_utilization": 0}],
    )

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("create_container", {"image_id": im.id, "gpu_count": 1})
    assert ok is True
    inst = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.user_id == u.id
    ).first()
    assert inst is not None
    assert f"id={inst.id}" in text
    assert f"port={inst.assigned_port}" in text
    assert inst.access_password
    assert f"password={inst.access_password}" in text
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.event == "create" and ev.source == "llm"
