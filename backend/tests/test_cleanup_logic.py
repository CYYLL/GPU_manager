"""Cleanup decision core: filters, constrained selection, TOCTOU, effectiveness."""
from datetime import datetime, timedelta
from unittest import mock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import cleanup


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


def _u(db, name="kilo", role="user"):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=8)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _i(db, u, status="stopped", protected=False, days_unused=10, cid=None):
    inst = models.ContainerInstance(
        user_id=u.id, container_id=cid or ("f" * 64), image="basic:v1",
        gpu_ids=[0], gpu_count=1, status=status, cleanup_protected=protected)
    db.add(inst); db.commit(); db.refresh(inst)
    db.add(models.ContainerEvent(container_instance_id=inst.id, user_id=u.id,
                                 event="create", source="manual",
                                 created_at=datetime.utcnow() - timedelta(days=days_unused)))
    db.commit()
    return inst


def test_candidates_exclude_protected_grace_running(monkeypatch, db):
    monkeypatch.setenv("GRACE_DAYS", "2")
    u = _u(db)
    old = _i(db, u, days_unused=10)                # stopped + unused → auto
    running = _i(db, u, status="running", days_unused=10, cid="g" * 64)  # → alert only
    recent = _i(db, u, days_unused=1, cid="h" * 64)          # grace → excluded
    prot = _i(db, u, protected=True, days_unused=10, cid="j" * 64)  # protected → nowhere
    p = cleanup.CleanupParams.from_env()
    auto, alert = cleanup.build_candidate_lists(db, {}, p)
    auto_ids = {x["id"] for x in auto}
    alert_ids = {x["id"] for x in alert}
    assert old.id in auto_ids
    assert running.id not in auto_ids and running.id in alert_ids
    assert recent.id not in auto_ids and recent.id not in alert_ids
    assert prot.id not in auto_ids and prot.id not in alert_ids


def test_rule_select_takes_first_k_lru():
    sel = cleanup.select_by_rule([{"id": 1}, {"id": 2}, {"id": 3}], 2)
    assert sel == [1, 2]


def test_validation_rejects_foreign_id():
    assert cleanup.validate_selection([1, 2], {1, 2, 3}) is True
    assert cleanup.validate_selection([1, 99], {1, 2, 3}) is False


def test_llm_select_fallback_when_no_tool_call(db):
    llm = mock.Mock()
    llm.complete.return_value = mock.Mock(tool_calls=[], text="no tool")
    sel = cleanup.llm_select_candidates(llm, [{"id": 1}, {"id": 2}], "ctx", cleanup.CleanupParams())
    assert sel is None  # caller falls back to rule


def test_llm_select_validates_subset(db):
    llm = mock.Mock()
    call = {"name": "select_cleanup_candidates", "input": {"ids": [1, 99]}}
    llm.complete.return_value = mock.Mock(tool_calls=[call], text="")
    sel = cleanup.llm_select_candidates(llm, [{"id": 1}], "ctx", cleanup.CleanupParams())
    assert sel is None  # 99 not in candidate ids → reject whole round


def test_toctou_detects_event_after_generation(db):
    u = _u(db)
    inst = _i(db, u, days_unused=10)
    generated_at = datetime.utcnow() - timedelta(minutes=1)
    db.add(models.ContainerEvent(container_instance_id=inst.id, user_id=u.id,
                                 event="start", source="manual"))  # after generated_at
    db.commit()
    ok, reason = cleanup.toctou_ok(db, inst.id, generated_at)
    assert ok is False and "changed" in reason


def test_toctou_ok_when_no_new_event(db):
    u = _u(db)
    inst = _i(db, u, days_unused=10)
    generated_at = datetime.utcnow() + timedelta(minutes=1)  # future: no event after it
    ok, reason = cleanup.toctou_ok(db, inst.id, generated_at)
    assert ok is True


def test_effectiveness_docker_trigger_low_free(db):
    p = cleanup.CleanupParams(min_effective_free_gb=5)
    ok, reason = cleanup.would_be_effective(2 * 1024 ** 3, p, triggered_by_docker=True)
    assert ok is False and "low" in reason.lower()


def test_effectiveness_ok_above_min(db):
    p = cleanup.CleanupParams(min_effective_free_gb=5)
    ok, _ = cleanup.would_be_effective(6 * 1024 ** 3, p, triggered_by_docker=True)
    assert ok is True


def test_alert_skips_when_open_capacity_exists(db):
    _u(db)
    db.add(models.AdminAlert(type="capacity", level="warning", message="first"))
    db.commit()
    cleanup.raise_capacity_alert(db, "second", "warning", {})
    msgs = [a.message for a in db.query(models.AdminAlert).all()]
    assert msgs == ["first"]  # no duplicate open alert
