"""Monitor/admin routes: mode guard, admin guard, live container status, disk
trend, cleanup log, capacity alerts (order + resolve), protection endpoint."""
from datetime import datetime
from unittest import mock
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
import app.routers.monitor as mon_r
import app.routers.containers as c_r


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def _user(db, name="nemo", role="user", mode="llm"):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=8, mode=mode)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_disk_requires_llm_mode(db):
    u = _user(db, mode="traditional")
    with pytest.raises(HTTPException) as e:
        mon_r.disk_status(u, db)
    assert e.value.status_code == 403


def test_candidates_requires_admin(db):
    u = _user(db, role="user", mode="llm")   # admin gate is in-body (direct-call tests bypass Depends)
    with pytest.raises(HTTPException) as e:
        mon_r.candidates(u, db)
    assert e.value.status_code == 403


def test_containers_live_status(monkeypatch, db):
    u = _user(db)
    inst = models.ContainerInstance(user_id=u.id, container_id="9" * 64, image="x:1",
                                    gpu_ids=[0], gpu_count=1, status="stopped",
                                    assigned_port=22001)
    db.add(inst)
    db.commit()
    runner = mock.Mock()
    runner.is_container_running.return_value = False
    monkeypatch.setattr(mon_r, "docker_runner", runner)
    rows = mon_r.containers(u, db)
    assert len(rows) == 1
    assert rows[0]["container_id"] == "9" * 12      # truncated for display
    assert rows[0]["docker_running"] is False
    assert rows[0]["status"] == "stopped"
    assert rows[0]["cleanup_protected"] is False
    assert rows[0]["assigned_port"] == 22001


def test_disk_status_returns_snapshot_trend(monkeypatch, db):
    u = _user(db)
    db.add(models.DiskSnapshot(total_bytes=100, used_bytes=70, free_bytes=30,
                               usage_percent=70.0,
                               docker_container_reclaimable_bytes=5,
                               docker_image_reclaimable_bytes=6,
                               docker_build_cache_reclaimable_bytes=7))
    db.commit()
    monkeypatch.setattr(mon_r, "_live_usage", lambda: (100, 72, 28, 72.0))
    out = mon_r.disk_status(u, db)
    assert out["latest"]["usage_percent"] == 70.0
    assert out["latest"]["docker_container_reclaimable_bytes"] == 5
    assert len(out["trend"]) == 1 and out["trend"][0]["usage_percent"] == 70.0
    assert out["live"]["usage_percent"] == 72.0


def test_cleanup_log_admin(db):
    u = _user(db, role="admin")
    db.add(models.CleanupLog(container_instance_id=1, user_id=u.id, action="remove",
                             reason="x", decision_source="llm", freed_bytes=10,
                             success=True))
    db.commit()
    rows = mon_r.cleanup_log(u, db)
    assert len(rows) == 1
    assert rows[0]["action"] == "remove" and rows[0]["decision_source"] == "llm"
    assert rows[0]["freed_bytes"] == 10 and rows[0]["success"] is True


def test_alerts_unresolved_first_and_resolve(db):
    u = _user(db, role="admin")
    old = models.AdminAlert(type="capacity", level="warning", message="old",
                            resolved_at=datetime.utcnow())
    db.add(old)
    db.commit()
    open_ = models.AdminAlert(type="capacity", level="critical", message="open")
    db.add(open_)
    db.commit()
    al = mon_r.alerts(u, db)
    assert [a["id"] for a in al] == [open_.id, old.id]   # unresolved first, then ts desc
    assert al[0]["resolved_at"] is None
    assert mon_r.resolve_alert(open_.id, u, db) == {"status": "resolved"}
    db.expire_all()                                      # prove the commit persisted
    al2 = mon_r.alerts(u, db)
    assert all(a["resolved_at"] is not None for a in al2)


def test_protection_endpoint_owner_only(db):
    owner = _user(db, name="one")
    other = _user(db, name="two")
    inst = models.ContainerInstance(user_id=owner.id, container_id="7" * 64, image="x:1",
                                    gpu_ids=[0], gpu_count=1, status="stopped")
    db.add(inst)
    db.commit()
    with pytest.raises(HTTPException) as e:
        c_r.set_protection(inst.id, mock.Mock(protected=True), other, db)
    assert e.value.status_code == 403
    out = c_r.set_protection(inst.id, mock.Mock(protected=True), owner, db)
    assert out == {"protected": True}
    db.expire_all()
    db.refresh(inst)
    assert inst.cleanup_protected is True
