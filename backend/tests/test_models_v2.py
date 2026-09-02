"""v2 data-model tests: new columns + new tables are created and usable."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import datetime

from app.database import Base
from app import models


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


def test_user_mode_column_defaults_llm(db):
    u = models.User(username="alice", hashed_password="x", role="user", gpu_quota=4)
    db.add(u)
    db.commit()
    assert u.mode == "llm"


def test_container_instance_v2_columns(db):
    u = models.User(username="bob", hashed_password="x", role="user", gpu_quota=4)
    db.add(u)
    db.commit()
    inst = models.ContainerInstance(
        user_id=u.id, container_id="c" * 64, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status="running",
        env_vars={"DEBUG": "1"}, cleanup_protected=True,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    assert inst.env_vars == {"DEBUG": "1"}
    assert inst.cleanup_protected is True


def test_container_event_roundtrip(db):
    u = models.User(username="carol", hashed_password="x", role="user", gpu_quota=4)
    db.add(u)
    db.commit()
    inst = models.ContainerInstance(
        user_id=u.id, container_id="d" * 64, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status="running",
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    ev = models.ContainerEvent(
        container_instance_id=inst.id, user_id=u.id,
        event="start", source="manual", detail="",
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    assert ev.event == "start"
    assert ev.source == "manual"


def test_admin_alert_roundtrip(db):
    a = models.AdminAlert(
        type="capacity", level="critical",
        message="disk full", meta={"used": 13},
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    assert a.level == "critical"
    assert a.resolved_at is None


def test_disk_snapshot_roundtrip(db):
    s = models.DiskSnapshot(
        total_bytes=100, used_bytes=74, free_bytes=26, usage_percent=74.0,
        docker_container_reclaimable_bytes=2, docker_image_reclaimable_bytes=20,
        docker_build_cache_reclaimable_bytes=278,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    assert s.usage_percent == 74.0
    assert s.docker_build_cache_reclaimable_bytes == 278
