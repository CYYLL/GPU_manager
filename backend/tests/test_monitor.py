"""monitor sample_once + retention_prune are deterministic and testable."""
from unittest import mock
from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import monitor as mon


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


def _mk_user(db, name="mia"):
    u = models.User(username=name, hashed_password="x", role="user", gpu_quota=8)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _mk_inst(db, u, status="running", cid=None):
    i = models.ContainerInstance(user_id=u.id, container_id=cid or ("0" * 64),
                                 image="basic:v1", gpu_ids=[0], gpu_count=1, status=status)
    db.add(i); db.commit(); db.refresh(i)
    return i


def test_sample_writes_snapshot_and_detects_external_stop(monkeypatch, db):
    u = _mk_user(db)
    dead = _mk_inst(db, u, status="running", cid="1" * 64)
    alive = _mk_inst(db, u, status="running", cid="2" * 64)
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=dead.id, user_id=u.id))
    db.commit()

    stub = type("DR", (), {"is_container_running": lambda self, cid: cid == "2" * 64})()
    monkeypatch.setattr(mon, "shutil", type("S", (), {
        "disk_usage": lambda p: type("U", (), {"total": 100, "used": 74, "free": 26})()})())

    n = mon.sample_once(db, stub)

    assert n == 1  # one external stop detected
    db.refresh(dead)
    assert dead.status == "stopped"
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == dead.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.event == "external_stop" and ev.source == "agent"
    snap = db.query(models.DiskSnapshot).order_by(models.DiskSnapshot.ts.desc()).first()
    assert snap is not None and snap.usage_percent == 74.0
    # allocations released for the dead one
    alloc = db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == dead.id).first()
    assert alloc.released_at is not None


def test_monitor_does_not_stop_container_when_docker_state_is_unknown(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=inst.id, user_id=u.id))
    db.commit()
    stub = type("DR", (), {"is_container_running": lambda self, cid: None})()
    monkeypatch.setattr(mon, "shutil", type("S", (), {
        "disk_usage": lambda p: type("U", (), {"total": 100, "used": 50, "free": 50})()})())

    assert mon.sample_once(db, stub) == 0
    db.refresh(inst)
    assert inst.status == "running"
    assert db.query(models.GpuAllocation).filter_by(container_instance_id=inst.id,
                                                   released_at=None).count() == 1


def test_retention_prune(monkeypatch, db):
    # retention cap is read from env (default 200) — pin it small so the test
    # actually exercises the per-user chat-history trim
    monkeypatch.setenv("CHAT_HISTORY_LIMIT", "2")
    u = _mk_user(db)
    # old event (60 days ago)
    db.add(models.ContainerEvent(container_instance_id=1, user_id=u.id,
                                 event="start", source="manual",
                                 created_at=datetime.utcnow() - timedelta(days=60)))
    # recent event
    db.add(models.ContainerEvent(container_instance_id=1, user_id=u.id,
                                 event="start", source="manual"))
    # old + recent snapshot
    db.add(models.DiskSnapshot(total_bytes=1, used_bytes=1, free_bytes=1, usage_percent=1.0,
                               ts=datetime.utcnow() - timedelta(days=10)))
    db.add(models.DiskSnapshot(total_bytes=1, used_bytes=1, free_bytes=1, usage_percent=1.0))
    # 3 chat messages over limit 2
    for i in range(3):
        db.add(models.ChatMessage(user_id=u.id, role="user", content=str(i)))
    db.commit()

    out = mon.retention_prune(db)

    assert db.query(models.ContainerEvent).count() == 1
    assert db.query(models.DiskSnapshot).count() == 1
    assert db.query(models.ChatMessage).filter(models.ChatMessage.user_id == u.id).count() == 2
    assert out["events_removed"] == 1 and out["snapshots_removed"] == 1
