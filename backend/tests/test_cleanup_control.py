"""Cleanup control loop: triggers, caps, cooldown, dry-run, escalation, prune cadence."""
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


@pytest.fixture()
def rule_llm():
    """LLM stub that never produces a decision → rule fallback path."""
    llm = mock.Mock()
    llm.complete.return_value = mock.Mock(tool_calls=[], text="")
    return llm


def _u(db, name="lima"):
    u = models.User(username=name, hashed_password="x", role="user", gpu_quota=8)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _stopped(db, u, cid, days_unused=10, protected=False):
    i = models.ContainerInstance(user_id=u.id, container_id=cid, image="basic:v1",
                                 gpu_ids=[0], gpu_count=1, status="stopped",
                                 cleanup_protected=protected)
    db.add(i); db.commit(); db.refresh(i)
    db.add(models.ContainerEvent(container_instance_id=i.id, user_id=u.id, event="create",
                                 source="manual", created_at=datetime.utcnow() - timedelta(days=days_unused)))
    db.commit()
    return i


def _disk_usage(pct):
    return type("U", (), {"total": 100, "used": pct, "free": 100 - pct})()


def _params(monkeypatch, p):
    """Force run_cleanup_cycle to read params p regardless of env; auto-restored."""
    monkeypatch.setattr(cleanup.CleanupParams, "from_env", classmethod(lambda cls: p))
    monkeypatch.setattr(cleanup, "workspace_bytes_provider", lambda: 0)


def _no_reclaim(df):
    return df


def test_disabled_returns_early(monkeypatch, db):
    _params(monkeypatch, cleanup.CleanupParams(enabled=False))
    out = cleanup.run_cleanup_cycle(runner=None, db=db)
    assert out["status"] == "disabled"


def test_dry_run_records_log_without_docker(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(dry_run=True, max_per_round=3))
    u = _u(db)
    inst = _stopped(db, u, "a" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [{"ID": "a" * 64, "ImageID": None, "SizeRw": 123}],
                              "Images": [], "BuildCache": []}
    runner.is_container_running.return_value = False
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(90)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    assert out["removed"] == 1
    db.refresh(inst)
    assert inst.status == "stopped"          # dry run: not marked removed
    runner.remove_container.assert_not_called()
    row = db.query(models.CleanupLog).filter(
        models.CleanupLog.container_instance_id == inst.id).first()
    assert row.action == "remove" and row.success is True and row.freed_bytes == 123


def test_image_prune_once_per_round_not_per_container(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(max_per_round=3))
    u = _u(db)
    _stopped(db, u, "b" * 64)
    _stopped(db, u, "c" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [
        {"ID": "b" * 64, "ImageID": "I1", "SizeRw": 10},
        {"ID": "c" * 64, "ImageID": "I1", "SizeRw": 10}],
        "Images": [], "BuildCache": []}
    runner.remove_container.return_value = (True, "removed")
    runner.is_container_running.return_value = False
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(90)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    assert out["removed"] == 2
    assert runner.image_prune.call_count == 1   # once after the round, not per container
    assert runner.remove_container.call_count == 2


def test_builder_prune_independent(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams())
    # nothing triggers container cleanup (all containers running → no reclaim)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [{"ID": "z" * 64, "ImageID": "I", "SizeRw": 1, "Running": True}],
                              "Images": [], "BuildCache": [{"Size": 150 * 1024 ** 3}]}
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(30)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    # builder prune triggered independently even with no container cleanup
    assert runner.build_cache_prune.call_count == 1
    assert out.get("build_cache_pruned") == runner.build_cache_prune.return_value


def test_cooldown_blocks_second_run(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(cooldown_minutes=5))
    db.add(models.CleanupLog(action="remove", success=True,
                             ts=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()
    out = cleanup.run_cleanup_cycle(runner=mock.Mock(), db=db, llm_client=rule_llm)
    assert out["status"] == "cooldown"


def test_dry_run_docker_trigger_no_escalation_alert(monkeypatch, db, rule_llm):
    # Docker-triggered round (usage below threshold): dry run frees nothing, so
    # the effectiveness gate must NOT raise a spurious capacity alert.
    _params(monkeypatch, cleanup.CleanupParams(
        dry_run=True, disk_threshold=90, container_reclaim_trigger_gb=1,
        min_effective_free_gb=5))
    u = _u(db)
    _stopped(db, u, "e" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [{"Id": "e" * 64, "ImageID": None,
                                              "SizeRw": 1 * 1024 ** 3, "State": "exited"}],
                              "Images": [], "BuildCache": []}
    runner.is_container_running.return_value = False
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(30)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    assert out["removed"] == 1
    assert out.get("escalated") is False
    assert db.query(models.AdminAlert).count() == 0


def test_real_docker_df_reclaim_does_not_raise_false_capacity_alert(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(
        disk_threshold=90, container_reclaim_trigger_gb=1,
        min_effective_free_gb=5))
    user = _u(db)
    inst = _stopped(db, user, "f" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [{
        "Id": inst.container_id, "ImageID": "sha256:tagged",
        "SizeRw": 6 * 1024 ** 3, "State": "exited"}],
        "Images": [{"Id": "sha256:tagged", "RepoTags": ["basic:v1"],
                    "Size": 20 * 1024 ** 3}], "BuildCache": []}
    runner.is_container_running.return_value = False
    runner.remove_container.return_value = (True, "removed")
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(30)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    assert out["removed"] == 1
    assert out["source_freed"] == 6 * 1024 ** 3
    assert out["escalated"] is False
    assert db.query(models.AdminAlert).count() == 0


def test_zero_estimate_below_disk_threshold_does_not_raise_capacity_alert(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(
        disk_threshold=85, container_reclaim_trigger_gb=1,
        min_effective_free_gb=5))
    user = _u(db)
    inst = _stopped(db, user, "g" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [
        {"Id": inst.container_id, "ImageID": None, "SizeRw": 0, "State": "exited"},
        {"Id": "h" * 64, "ImageID": None, "SizeRw": 6 * 1024 ** 3,
         "State": "exited"}], "Images": [], "BuildCache": []}
    runner.is_container_running.return_value = False
    runner.remove_container.return_value = (True, "removed")
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(69)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    assert out["removed"] == 1
    assert out["source_freed"] == 0
    assert out["escalated"] is False
    assert db.query(models.AdminAlert).count() == 0


def test_zero_estimate_above_disk_threshold_uses_accurate_alert(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(disk_threshold=85))
    user = _u(db)
    inst = _stopped(db, user, "i" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [
        {"Id": inst.container_id, "ImageID": None, "SizeRw": 0,
         "State": "exited"}], "Images": [], "BuildCache": []}
    runner.is_container_running.return_value = False
    runner.remove_container.return_value = (True, "removed")
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(90)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    alert = db.query(models.AdminAlert).filter(models.AdminAlert.type == "capacity").first()
    assert out["escalated"] is True
    assert alert is not None
    assert "本轮容器清理估算回收 0 字节" in alert.message
    assert "无更多可回收空间" not in alert.message


def test_daily_cap_blocks(monkeypatch, db, rule_llm):
    _params(monkeypatch, cleanup.CleanupParams(max_per_day=2, cooldown_minutes=0))
    today = cleanup._start_of_today()
    for i in range(2):
        db.add(models.CleanupLog(action="remove", success=True, ts=today + timedelta(minutes=i)))
    db.commit()
    out = cleanup.run_cleanup_cycle(runner=mock.Mock(), db=db, llm_client=rule_llm)
    assert out["status"] == "daily_cap"


def test_capacity_escalation_when_workspace_dominant(monkeypatch, db, rule_llm):
    p = cleanup.CleanupParams(disk_threshold=85, workspace_dominant_pct=60)
    monkeypatch.setattr(cleanup.CleanupParams, "from_env", classmethod(lambda cls: p))
    monkeypatch.setattr(cleanup, "workspace_bytes_provider", lambda: 900)  # workspace 900/used 90 → dominant
    u = _u(db)
    _stopped(db, u, "d" * 64)
    runner = mock.Mock()
    runner.df.return_value = {"Containers": [{"ID": "d" * 64, "ImageID": "I", "SizeRw": 10}],
                              "Images": [], "BuildCache": []}
    runner.remove_container.return_value = (True, "removed")
    runner.is_container_running.return_value = False
    with mock.patch.object(cleanup.shutil, "disk_usage", return_value=_disk_usage(90)):
        out = cleanup.run_cleanup_cycle(runner=runner, db=db, llm_client=rule_llm)
    alert = db.query(models.AdminAlert).filter(models.AdminAlert.type == "capacity").first()
    assert alert is not None and alert.level == "warning"
    assert out.get("escalated") is True
