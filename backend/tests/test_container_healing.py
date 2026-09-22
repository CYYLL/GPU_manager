"""Regression tests for DB <-> Docker container state consistency.

Covers the multi-user failure where the backend Docker container is actually
running but the DB record (and therefore the frontend status, the Start action,
and the GPU user attribution) says the container is stopped.

The route functions are invoked directly (bypassing the ASGI layer) with a
real in-memory DB session and mocked Docker/NVML singletons.
"""
import pytest
from unittest import mock
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
import app.routers.containers as containers_router

TEST_GPU_IDS = [0]


def _make_instance(db, user_id, status="running"):
    inst = models.ContainerInstance(
        user_id=user_id,
        container_id="deadbeefdeadbeefdeadbeefdeadbeef",
        image="basic:v1",
        gpu_ids=list(TEST_GPU_IDS),
        gpu_count=1,
        status=status,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _active_alloc(db, instance_id):
    return db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == instance_id,
        models.GpuAllocation.released_at.is_(None),
    ).all()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def test_user(db):
    u = models.User(username="alice", hashed_password="x", role="user", gpu_quota=4)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def docker_mock():
    m = mock.Mock()
    containers_router.docker_runner = m
    return m


@pytest.fixture()
def gpu_mock():
    m = mock.Mock()
    m.get_gpu_count.return_value = 4
    m.get_gpu_status.return_value = [
        {"id": i, "memory_utilization": 0.0, "gpu_utilization": 0}
        for i in range(4)
    ]
    containers_router.gpu_monitor = m
    return m


# ── 1. Stop must not corrupt state if the Docker stop fails ──────────────────


def test_stop_marks_nothing_stopped_when_docker_stop_fails_and_container_still_running(
    db, test_user, docker_mock
):
    inst = _make_instance(db, test_user.id, status="running")
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=inst.id, user_id=test_user.id))
    db.commit()

    containers_router.docker_runner.stop_container.return_value = (
        False,
        "docker daemon timeout",
    )
    # The container survived the failed stop.
    containers_router.docker_runner.is_container_running.return_value = True

    with pytest.raises(HTTPException) as excinfo:
        containers_router.stop_container(inst.id, test_user, db)

    # The stop failed, so the backend must NOT pretend the container is stopped.
    assert excinfo.value.status_code == 500
    db.refresh(inst)
    assert inst.status == "running"
    assert len(_active_alloc(db, inst.id)) == 1


# ── 2. List sync heals a stopped DB record whose Docker container is running ──


def test_list_containers_heals_stopped_instance_that_is_running_in_docker(
    db, test_user, docker_mock
):
    inst = _make_instance(db, test_user.id, status="stopped")
    # Docker says the container is alive, DB says stopped.
    containers_router.docker_runner.is_container_running.return_value = True

    result = containers_router.list_containers(test_user, db)

    assert len(result) == 1
    assert result[0].status == "running"
    db.refresh(inst)
    assert inst.status == "running"
    assert len(_active_alloc(db, inst.id)) == 1


def test_list_does_not_mark_running_container_stopped_when_docker_is_unavailable(
    db, test_user, docker_mock
):
    inst = _make_instance(db, test_user.id, status="running")
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=inst.id, user_id=test_user.id))
    db.commit()
    docker_mock.is_container_running.return_value = None

    result = containers_router.list_containers(test_user, db)

    assert result[0].status == "running"
    db.refresh(inst)
    assert inst.status == "running"
    assert len(_active_alloc(db, inst.id)) == 1


# ── 3. Start heals an already-running container instead of failing ───────────


def test_start_stopped_heals_when_docker_already_running(db, test_user, docker_mock, gpu_mock):
    inst = _make_instance(db, test_user.id, status="stopped")
    # Docker container is actually running and using its GPU (NVML shows busy).
    containers_router.docker_runner.is_container_running.return_value = True
    containers_router.gpu_monitor.get_gpu_status.return_value = [
        {"id": 0, "memory_utilization": 60.0, "gpu_utilization": 50}
    ]

    result = containers_router.start_stopped_container(inst.id, test_user, db)

    assert result["message"] == "Container is already running"
    db.refresh(inst)
    assert inst.status == "running"
    assert len(_active_alloc(db, inst.id)) == 1


def test_start_stopped_repairs_ssh_before_healing_running_docker(
    db, test_user, docker_mock, gpu_mock
):
    inst = _make_instance(db, test_user.id, status="stopped")
    inst.access_password = "secret"
    inst.ssh_username = "root"
    db.commit()
    docker_mock.is_container_running.return_value = True
    docker_mock.ensure_ssh.return_value = (True, "root", "SSH 登录已就绪")

    result = containers_router.start_stopped_container(inst.id, test_user, db)

    assert result["message"] == "Container is already running"
    docker_mock.ensure_ssh.assert_called_once_with(inst.container_id, "secret", "root")
    db.refresh(inst)
    assert inst.status == "running"


def test_start_stopped_does_not_claim_success_when_ssh_repair_fails(
    db, test_user, docker_mock, gpu_mock
):
    inst = _make_instance(db, test_user.id, status="stopped")
    inst.access_password = "secret"
    db.commit()
    docker_mock.is_container_running.return_value = True
    docker_mock.ensure_ssh.return_value = (False, None, "SSH 服务未在映射端口就绪")

    with pytest.raises(HTTPException) as excinfo:
        containers_router.start_stopped_container(inst.id, test_user, db)

    assert excinfo.value.status_code == 503
    db.refresh(inst)
    assert inst.status == "stopped"
    assert not _active_alloc(db, inst.id)
