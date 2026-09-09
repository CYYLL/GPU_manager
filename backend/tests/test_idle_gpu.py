"""idle_gpu scan: idle-GPU tracking, window-based auto-stop, notice + event."""
from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import idle_gpu as idle

T0 = datetime(2026, 1, 1, 0, 0, 0)
P = idle.IdleGpuParams(enabled=True, dry_run=False, idle_hours=8.0, memory_idle_pct=5.0)


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


def _mk_inst(db, u, gpu_ids=None, status="running", protected=False, cid=None):
    inst = models.ContainerInstance(
        user_id=u.id, container_id=cid or ("0" * 64),
        image="basic:v1", gpu_ids=gpu_ids or [0], gpu_count=len(gpu_ids or [0]),
        status=status, cleanup_protected=protected,
    )
    db.add(inst); db.commit(); db.refresh(inst)
    return inst


def _alloc(db, inst, gpu_id):
    db.add(models.GpuAllocation(gpu_id=gpu_id, container_instance_id=inst.id,
                                user_id=inst.user_id))
    db.commit()


def _g(id, util=0, mem=0.0):
    return {"id": id, "gpu_utilization": util, "memory_utilization": mem}


class _Gpu:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
    def get_gpu_status(self):
        return list(self.statuses)


class _GpuSeq:
    """Per-call result queue — exercises the TOCTOU second confirmation: a scan
    sees idle (candidate), but the fresh pre-stop re-sample is busy."""
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0
    def get_gpu_status(self):
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return list(self.results[idx])


class _DR:
    fail = False
    def __init__(self):
        self.stopped = []
    def stop_container(self, cid):
        if self.fail:
            return False, "docker timeout"
        self.stopped.append(cid)
        return True, "ok"
    def is_container_running(self, cid):
        return True


def _events(db, inst_id):
    return db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst_id
    ).order_by(models.ContainerEvent.created_at.desc()).all()


# ── guardrails: disabled / unreadable / unknown sample ──────────────────────

def test_disabled_does_nothing(db):
    u = _mk_user(db)
    _mk_inst(db, u)
    dr = _DR()
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), dr, now=T0,
                              p=idle.IdleGpuParams(enabled=False))
    assert out["status"] == "disabled"
    assert db.query(models.ContainerIdleState).count() == 0
    assert dr.stopped == []


def test_unreadable_never_stops(db):
    u = _mk_user(db)
    _mk_inst(db, u)
    out = idle.scan_idle_gpus(db, _Gpu(), _DR(), now=T0, p=P)
    assert out["status"] == "unreadable"
    assert out["stopped"] == 0


def test_missing_gpu_sample_never_stops(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, gpu_ids=[0])
    # 先正常空闲两个周期，idle_since=T0
    idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0, p=P)
    idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0 + timedelta(hours=1), p=P)
    # 之后采样里查不到 gpu0 → 本周期无法下结论，绝不停止
    out = idle.scan_idle_gpus(db, _Gpu(_g(9, util=0, mem=0)), _DR(),
                              now=T0 + timedelta(hours=9), p=P)
    assert out["stopped"] == 0
    db.refresh(inst)
    assert inst.status == "running"


# ── idle tracking: busy no-op, first tick sets marker, reset on busy ────────

def test_busy_never_sets_idle_marker(db):
    u = _mk_user(db)
    _mk_inst(db, u)
    out = idle.scan_idle_gpus(db, _Gpu(_g(0, util=30, mem=12.0)), _DR(), now=T0, p=P)
    assert out["checked"] == 1 and out["idle_starts"] == 0
    assert db.query(models.ContainerIdleState).count() == 0


def test_first_idle_tick_marks_not_stop(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    _alloc(db, inst, 0)
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0, p=P)
    assert out["idle_starts"] == 1 and out["stopped"] == 0
    row = db.query(models.ContainerIdleState).filter(
        models.ContainerIdleState.container_instance_id == inst.id).first()
    assert row is not None and row.idle_since == T0
    db.refresh(inst)
    assert inst.status == "running"


def test_busy_resets_idle_accumulation(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0, p=P)
    assert db.query(models.ContainerIdleState).count() == 1
    # 中途忙了一下 → 标记清除
    out = idle.scan_idle_gpus(db, _Gpu(_g(0, util=10, mem=3.0)), _DR(),
                              now=T0 + timedelta(hours=2), p=P)
    assert out["busy_reset"] == 1
    assert db.query(models.ContainerIdleState).count() == 0
    # 再次空闲要从"重新开始"算，即便离首次已超 8h 也不停
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0 + timedelta(hours=9), p=P)
    assert out["idle_starts"] == 1 and out["stopped"] == 0
    db.refresh(inst)
    assert inst.status == "running"


# ── window & multi-GPU semantics ────────────────────────────────────────────

def test_stops_after_window_and_records_event_and_notice(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    _alloc(db, inst, 0)
    dr = _DR()
    idle.scan_idle_gpus(db, _Gpu(_g(0)), dr, now=T0, p=P)
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), dr,
                              now=T0 + timedelta(hours=8), p=P)

    assert out["stopped"] == 1 and out["notified"] == 1
    assert dr.stopped == [inst.container_id]
    db.refresh(inst)
    assert inst.status == "stopped"
    # allocation released
    a = db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == inst.id).first()
    assert a.released_at is not None
    # event
    evs = _events(db, inst.id)
    assert evs and evs[0].event == "stop" and evs[0].source == "agent"
    assert "8" in evs[0].detail and "自动停止" in evs[0].detail
    # notice inserted into the owner's chat history
    note = db.query(models.ChatMessage).filter(
        models.ChatMessage.user_id == u.id).order_by(
        models.ChatMessage.created_at.desc()).first()
    assert note.role == "assistant" and "系统通知" in note.content and "id=%d" % inst.id in note.content
    # idle marker cleaned
    assert db.query(models.ContainerIdleState).filter(
        models.ContainerIdleState.container_instance_id == inst.id).count() == 0


def test_protected_container_is_not_exempt(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, protected=True)
    _alloc(db, inst, 0)
    idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0, p=P)
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(),
                              now=T0 + timedelta(hours=8), p=P)
    assert out["stopped"] == 1  # 选定：不豁免保护容器
    db.refresh(inst)
    assert inst.status == "stopped"


def test_multigpu_stops_only_when_all_cards_idle(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, gpu_ids=[0, 1])
    for g in (0, 1):
        _alloc(db, inst, g)
    # 卡1 忙 → 不 idle
    idle.scan_idle_gpus(db, _Gpu(_g(0), _g(1, util=40, mem=5.0)), _DR(), now=T0, p=P)
    assert db.query(models.ContainerIdleState).count() == 0
    # 两卡都空 → idle
    idle.scan_idle_gpus(db, _Gpu(_g(0), _g(1)), _DR(),
                        now=T0 + timedelta(hours=1), p=P)
    assert db.query(models.ContainerIdleState).count() == 1
    out = idle.scan_idle_gpus(db, _Gpu(_g(0), _g(1)), _DR(),
                              now=T0 + timedelta(hours=9), p=P)
    assert out["stopped"] == 1
    db.refresh(inst)
    assert inst.status == "stopped"


def test_memory_high_alone_keeps_not_idle(db):
    u = _mk_user(db)
    _mk_inst(db, u)
    out = idle.scan_idle_gpus(db, _Gpu(_g(0, util=0, mem=60.0)), _DR(), now=T0, p=P)
    assert out["idle_starts"] == 0
    assert db.query(models.ContainerIdleState).count() == 0


def test_second_confirmation_sees_busy_and_skips_stop(db):
    """8h 判定通过后、动作前那一刻已不空闲 → 不停止（清标记重新累计）。"""
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    dr = _DR()
    # call1: 首个 idle tick → 写标记（idle_since=T0）
    # call2: +8h 主采样仍空闲 → hours>=8 判为候选
    # call3: 候选后动作前新鲜复检 → 已忙 → 不清标记直接跳过
    gpu = _GpuSeq([_g(0)], [_g(0)], [_g(0, util=90, mem=20.0)])
    idle.scan_idle_gpus(db, gpu, dr, now=T0, p=P)
    out = idle.scan_idle_gpus(db, gpu, dr, now=T0 + timedelta(hours=8), p=P)
    assert out["stopped"] == 0
    assert gpu.calls == 3  # 主采样 + 每个候选一次新鲜复检
    db.refresh(inst)
    assert inst.status == "running"
    # 复检发现忙碌 → 标记被清除，下轮重新累计
    assert db.query(models.ContainerIdleState).count() == 0


# ── dry-run & failure ────────────────────────────────────────────────────────

def test_dry_run_plans_but_does_not_stop(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    _alloc(db, inst, 0)
    dr = _DR()
    p_dry = idle.IdleGpuParams(enabled=True, dry_run=True, idle_hours=8.0,
                               memory_idle_pct=5.0)
    idle.scan_idle_gpus(db, _Gpu(_g(0)), dr, now=T0, p=p_dry)
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), dr,
                              now=T0 + timedelta(hours=8), p=p_dry)
    assert out["planned"] == 1 and out["stopped"] == 0
    assert dr.stopped == []
    db.refresh(inst)
    assert inst.status == "running"
    assert db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id).count() == 0


def test_stop_failure_leaves_running_and_logs_error(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    idle.scan_idle_gpus(db, _Gpu(_g(0)), _DR(), now=T0, p=P)
    dr = _DR()
    dr.fail = True
    out = idle.scan_idle_gpus(db, _Gpu(_g(0)), dr,
                              now=T0 + timedelta(hours=8), p=P)
    assert out["stopped"] == 0
    assert len(out["errors"]) == 1
    db.refresh(inst)
    assert inst.status == "running"
    assert db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id).count() == 0
    # 标记已被抢占删除 → 不会热重试，下轮从零累计
    assert db.query(models.ContainerIdleState).filter(
        models.ContainerIdleState.container_instance_id == inst.id).count() == 0
