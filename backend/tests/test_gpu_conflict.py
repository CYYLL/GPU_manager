"""GPU 冲突拒绝规则：同一张卡只能被一台容器占用。

同一用户的多个容器配置（stopped / removed 快照）可能都记着同一张卡（例：GPU 1）。
当该卡已被另一台运行中的容器占用（DB 有活动分配）或正被未登记进程使用（NVML 有负载）时，
启动(stopped→running) 与重建(removed→running) 都必须被 400 拒绝，且原因要能定位占用方。
放行路径（卡空闲）也必须照常工作。REST 与 agent 共用同一 _impl。
"""
import pytest
from unittest import mock
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
import app.routers.containers as C

_FREE_GPUS = [
    {"id": i, "memory_utilization": 0.0, "gpu_utilization": 0} for i in range(4)
]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def _user(db, name, role="user"):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=4)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk(db, user_id, cid, status="stopped", gpu_ids=(1,), port=None):
    inst = models.ContainerInstance(
        user_id=user_id, container_id=cid, image="basic:v1",
        gpu_ids=list(gpu_ids), gpu_count=len(gpu_ids), status=status,
        assigned_port=port, access_password="pw",
    )
    db.add(inst)
    db.commit()
    return inst.id  # commit 会过期对象 → 只回传整数 id


def _grant(db, gpu_id, inst_id, user_id):
    db.add(models.GpuAllocation(gpu_id=gpu_id, container_instance_id=inst_id, user_id=user_id))
    db.commit()


@pytest.fixture()
def docker_mock():
    m = mock.Mock()
    m.is_container_running.return_value = False
    m.start_container_by_id.return_value = (True, "running")
    m.start_container.return_value = ("d" * 64, "running", "root")
    m.remove_container.return_value = (True, "removed")
    C.docker_runner = m
    return m


@pytest.fixture()
def gpu_mock():
    m = mock.Mock()
    m.get_gpu_count.return_value = 4
    m.get_gpu_status.return_value = list(_FREE_GPUS)
    C.gpu_monitor = m
    return m


# ── restart (stopped→running)：卡被另一容器占用 / NVML 忙 / 空闲放行 ────────────

def test_restart_refuses_when_gpu_claimed_by_another_container(db, docker_mock, gpu_mock):
    u = _user(db, "alice")
    running_id = _mk(db, u.id, "a" * 64, status="running", gpu_ids=(1,), port=22000)
    _grant(db, 1, running_id, u.id)
    stopped_id = _mk(db, u.id, "b" * 64, status="stopped", gpu_ids=(1,), port=22001)

    with pytest.raises(HTTPException) as ei:
        C._start_stopped_container_impl(stopped_id, u, db, "manual")

    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert "GPU 1" in detail
    assert "container " in detail and "alice" in detail  # 能定位占用方容器与用户
    # 未被放行：仍是 stopped、无活动分配
    inst = db.query(models.ContainerInstance).filter(models.ContainerInstance.id == stopped_id).first()
    assert inst.status == "stopped"
    assert db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == stopped_id,
        models.GpuAllocation.released_at.is_(None)).count() == 0


def test_restart_refuses_when_gpu_busy_untracked(db, docker_mock, gpu_mock):
    u = _user(db, "alice")
    stopped_id = _mk(db, u.id, "b" * 64, status="stopped", gpu_ids=(1,), port=22000)
    # GPU 1 有活动负载但库中无分配（外部/未登记进程占用）
    gpu_mock.get_gpu_status.return_value = [
        {"id": 0, "memory_utilization": 0.0, "gpu_utilization": 0},
        {"id": 1, "memory_utilization": 60.0, "gpu_utilization": 50},
    ]

    with pytest.raises(HTTPException) as ei:
        C._start_stopped_container_impl(stopped_id, u, db, "manual")

    assert ei.value.status_code == 400
    assert "GPU 1" in ei.value.detail and "未登记" in ei.value.detail
    inst = db.query(models.ContainerInstance).filter(models.ContainerInstance.id == stopped_id).first()
    assert inst.status == "stopped"


def test_restart_allows_when_gpu_free(db, docker_mock, gpu_mock):
    u = _user(db, "alice")
    stopped_id = _mk(db, u.id, "b" * 64, status="stopped", gpu_ids=(1,), port=22000)

    res = C._start_stopped_container_impl(stopped_id, u, db, "manual")

    assert res["message"] == "Container started successfully"
    docker_mock.start_container_by_id.assert_called_once()
    inst = db.query(models.ContainerInstance).filter(models.ContainerInstance.id == stopped_id).first()
    assert inst.status == "running"
    assert db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == stopped_id,
        models.GpuAllocation.released_at.is_(None)).count() == 1


# ── rebuild (removed→running)：冲突拒绝 / 空闲放行 ──────────────────────────────

def test_rebuild_refuses_when_gpu_claimed_by_another_container(db, docker_mock, gpu_mock):
    u = _user(db, "alice")
    running_id = _mk(db, u.id, "a" * 64, status="running", gpu_ids=(1,), port=22000)
    _grant(db, 1, running_id, u.id)
    removed_id = _mk(db, u.id, "c" * 64, status="removed", gpu_ids=(1,), port=None)

    with pytest.raises(HTTPException) as ei:
        C._rebuild_container_impl(removed_id, u, db, "manual")

    assert ei.value.status_code == 400
    assert "GPU 1" in ei.value.detail
    inst = db.query(models.ContainerInstance).filter(models.ContainerInstance.id == removed_id).first()
    assert inst.status == "removed"  # 重建失败：快照保留，未误启动


def test_rebuild_allows_when_gpu_free(db, docker_mock, gpu_mock):
    u = _user(db, "alice")
    removed_id = _mk(db, u.id, "c" * 64, status="removed", gpu_ids=(1,), port=None)

    resp = C._rebuild_container_impl(removed_id, u, db, "manual")

    assert resp.status == "running"
    inst = db.query(models.ContainerInstance).filter(models.ContainerInstance.id == removed_id).first()
    assert inst.status == "running"
    assert inst.assigned_port is not None  # 快照释放的端口已重新分配
    assert db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == removed_id,
        models.GpuAllocation.released_at.is_(None)).count() == 1
