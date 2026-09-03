"""Lifecycle handlers accept a source param recorded into ContainerEvent."""
from unittest import mock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
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


def _mk(db, user, status="running"):
    inst = models.ContainerInstance(
        user_id=user.id, container_id="f" * 64, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status=status,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def test_stop_records_source_llm(monkeypatch, db):
    u = models.User(username="sam", hashed_password="x", role="user", gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    inst = _mk(db, u)

    stub = mock.Mock()
    stub.stop_container.return_value = (True, "stopped")
    stub.is_container_running.return_value = False
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    containers_router.stop_container(inst.id, u, db, source="llm")

    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.event == "stop"
    assert ev.source == "llm"


def test_remove_records_source_manual_default(monkeypatch, db):
    u = models.User(username="tina", hashed_password="x", role="user", gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    inst = _mk(db, u)

    stub = mock.Mock()
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    containers_router.remove_container(inst.id, u, db)  # default source="manual"

    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.source == "manual"
