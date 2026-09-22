"""Rebuild: removed containers are recreated from their config snapshot."""
from unittest import mock
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.crud import containers as crud
import app.routers.containers as containers_router


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


def _mk(db, u, status="running", cid=None, port=22010):
    i = models.ContainerInstance(user_id=u.id, container_id=cid or ("a" * 64),
                                 image="basic:v1", gpu_ids=[0], gpu_count=1,
                                 status=status, assigned_port=port,
                                 access_password="pw123", env_vars={"DEBUG": "1"})
    db.add(i); db.commit(); db.refresh(i)
    return i


def _mk_user(db, name="nala", role="user"):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=8)
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_mark_container_removed(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk(db, u)
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=inst.id, user_id=u.id))
    db.commit()
    stub = mock.Mock()
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ok = crud.mark_container_removed(db, inst.id, docker_runner=stub)

    assert ok is True
    db.refresh(inst)
    assert inst.status == "removed"
    assert inst.assigned_port is None  # 清理释放端口，removed 快照不再占端口
    alloc = db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == inst.id).first()
    assert alloc.released_at is not None


def test_mark_container_removed_keeps_state_when_docker_remove_fails(db):
    """A container docker could not actually remove must stay tracked — cleanup
    must never claim success (or release GPUs) for a container still alive."""
    u = _mk_user(db)
    inst = _mk(db, u, status="stopped", cid="e" * 64)
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=inst.id, user_id=u.id))
    db.commit()
    stub = mock.Mock()
    stub.remove_container.return_value = (False, "docker daemon down")

    ok = crud.mark_container_removed(db, inst.id, docker_runner=stub)

    assert ok is False
    db.refresh(inst)
    assert inst.status == "stopped"  # untouched, still rebuildable/retryable
    alloc = db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == inst.id).first()
    assert alloc.released_at is None


def test_rebuild_requires_removed_status(monkeypatch, db):
    u = _mk_user(db)
    running = _mk(db, u, status="running", cid="b" * 64)
    with pytest.raises(HTTPException) as e:
        containers_router._rebuild_container_impl(running.id, u, db, "manual")
    assert e.value.status_code == 400


def test_rebuild_recreates_from_snapshot(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk(db, u, status="removed", cid="c" * 64)
    img = models.GpuImage(name="basic", image="basic:v1", min_gpu=1)
    db.add(img); db.commit()

    dr = mock.Mock()
    dr.is_container_running.return_value = False
    dr.get_running_gpu_claims.return_value = {}
    dr.start_container.return_value = ("d" * 64, "running", "root")
    monkeypatch.setattr(containers_router, "docker_runner", dr)
    # GPU monitor idle + allocator
    gpu_mock = mock.Mock()
    gpu_mock.get_gpu_count.return_value = 4
    gpu_mock.get_gpu_status.return_value = [
        {"id": 0, "memory_utilization": 0.0, "gpu_utilization": 0},
        {"id": 1, "memory_utilization": 0.0, "gpu_utilization": 0},
    ]
    monkeypatch.setattr(containers_router, "gpu_monitor", gpu_mock)
    monkeypatch.setattr("app.routers.containers.GPUAllocator", lambda **kw: mock.Mock(
        allocate_guard=mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: None),
        check_quota=lambda *a, **k: (True, ""),
        find_available_gpus=lambda *a, **k: [0],
    ))
    monkeypatch.setenv("CONTAINER_MOUNT_ROOT", "/tmp")

    resp = containers_router._rebuild_container_impl(inst.id, u, db, "manual")

    assert resp.status == "running"
    db.refresh(inst)
    assert inst.status == "running"
    assert inst.container_id == "d" * 64
    assert inst.env_vars == {"DEBUG": "1"}  # snapshot preserved
    # start_container got the stored image/env/password
    call = dr.start_container.call_args
    assert call.kwargs["image"] == "basic:v1"
    assert call.kwargs["env_vars"] == {"DEBUG": "1"}
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.event == "rebuild"


def test_rebuild_allocates_fresh_port_when_snapshot_freed(monkeypatch, db):
    """清理释放端口(assigned_port=None)后的快照重建 → 重新分配一个空闲端口，不再复用旧端口。"""
    u = _mk_user(db)
    inst = _mk(db, u, status="removed", cid="f" * 64, port=None)  # Task1 后清理产出的快照形态
    img = models.GpuImage(name="basic", image="basic:v1", min_gpu=1)
    db.add(img)
    db.commit()

    dr = mock.Mock()
    dr.is_container_running.return_value = False
    dr.get_running_gpu_claims.return_value = {}
    dr.start_container.return_value = ("d" * 64, "running", "root")
    monkeypatch.setattr(containers_router, "docker_runner", dr)
    gpu_mock = mock.Mock()
    gpu_mock.get_gpu_count.return_value = 4
    gpu_mock.get_gpu_status.return_value = [
        {"id": 0, "memory_utilization": 0.0, "gpu_utilization": 0},
        {"id": 1, "memory_utilization": 0.0, "gpu_utilization": 0},
    ]
    monkeypatch.setattr(containers_router, "gpu_monitor", gpu_mock)
    monkeypatch.setattr("app.routers.containers.GPUAllocator", lambda **kw: mock.Mock(
        allocate_guard=mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: None),
        check_quota=lambda *a, **k: (True, ""),
        find_available_gpus=lambda *a, **k: [0],
    ))
    monkeypatch.setenv("CONTAINER_MOUNT_ROOT", "/tmp")

    resp = containers_router._rebuild_container_impl(inst.id, u, db, "manual")

    assert resp.status == "running"
    db.refresh(inst)
    assert inst.status == "running"
    assert inst.assigned_port == 22000  # 空池重新分配到最小空闲端口
    assert dr.start_container.call_args.kwargs["ports"] == {"22/tcp": "22000"}
