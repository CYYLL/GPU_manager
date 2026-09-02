"""Lifecycle events are recorded; last_used derives from the latest event."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.crud import containers as crud


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


def _mk(db, user, status="running"):
    inst = models.ContainerInstance(
        user_id=user.id, container_id="e" * 64, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status=status,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def test_record_event_and_last_used(db):
    u = models.User(username="dave", hashed_password="x", role="user", gpu_quota=4)
    db.add(u)
    db.commit()
    db.refresh(u)
    inst = _mk(db, u)

    crud.record_container_event(db, inst.id, u.id, "create", "manual", "initial")
    crud.record_container_event(db, inst.id, u.id, "start", "llm", "via chat")

    events = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at).all()
    assert [e.event for e in events] == ["create", "start"]
    assert events[1].source == "llm"
    assert crud.get_last_used(db, inst.id) == events[1].created_at


def test_get_last_used_none_when_no_events(db):
    u = models.User(username="erin", hashed_password="x", role="user", gpu_quota=4)
    db.add(u)
    db.commit()
    db.refresh(u)
    inst = _mk(db, u)
    assert crud.get_last_used(db, inst.id) is None
