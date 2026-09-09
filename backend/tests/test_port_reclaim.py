"""allocate_port 低水位自动回收：仅回收最旧 removed 快照、exclude_id 保护正在重建的行。"""
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.crud import containers as crud


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def _u(db, name):
    u = models.User(username=name, hashed_password="x", role="user", gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk(db, u, status="running", port=None, cid="a" * 64, days=None):
    i = models.ContainerInstance(user_id=u.id, container_id=cid, image="basic:v1",
                                 gpu_ids=[0], gpu_count=1, status=status, assigned_port=port)
    if days is not None:
        i.stopped_at = datetime.utcnow() - timedelta(days=days)
    db.add(i)
    db.commit()
    db.refresh(i)
    return i


def test_allocate_reclaims_oldest_removed_only(monkeypatch, db):
    monkeypatch.setattr(crud, "PORT_RANGE_START", 22000)
    monkeypatch.setattr(crud, "PORT_RANGE_END", 22004)   # 5 个端口
    monkeypatch.setattr(crud, "PORT_RECLAIM_THRESHOLD", 1)
    u = _u(db, "x")
    _mk(db, u, port=22000, cid="1" * 64)                    # running：永不动
    old = _mk(db, u, status="removed", port=22001, cid="2" * 64, days=6)
    mid = _mk(db, u, status="removed", port=22002, cid="3" * 64, days=4)
    recent = _mk(db, u, status="removed", port=22003, cid="4" * 64, days=1)
    _mk(db, u, port=22004, cid="5" * 64)                    # running：永不动
    # commit 会过期对象 → 先捕获原始 id（整数），之后只按 id 查询、不触碰对象
    old_id, mid_id, recent_id = old.id, mid.id, recent.id
    # 空闲=0 ≤1 → 回收最旧两个(22001,22002) → 空闲=2>1 停止；返回最小空闲 22001
    assert crud.allocate_port(db) == 22001
    assert db.query(models.ContainerInstance).filter(models.ContainerInstance.id == old_id).first() is None
    assert db.query(models.ContainerInstance).filter(models.ContainerInstance.id == mid_id).first() is None
    assert db.query(models.ContainerInstance).filter(models.ContainerInstance.id == recent_id).first() is not None
    # running 行完好
    assert db.query(models.ContainerInstance).filter(
        models.ContainerInstance.status == "running").count() == 2


def test_allocate_reclaim_excludes_current_snapshot(monkeypatch, db):
    monkeypatch.setattr(crud, "PORT_RANGE_START", 22000)
    monkeypatch.setattr(crud, "PORT_RANGE_END", 22003)   # 4 个端口
    monkeypatch.setattr(crud, "PORT_RECLAIM_THRESHOLD", 1)
    u = _u(db, "y")
    _mk(db, u, port=22000, cid="1" * 64)
    current = _mk(db, u, status="removed", port=22001, cid="2" * 64, days=9)  # 正被 rebuild 的行
    other = _mk(db, u, status="removed", port=22002, cid="3" * 64, days=5)
    _mk(db, u, port=22003, cid="4" * 64)
    current_id, other_id = current.id, other.id
    # 排除 current：只删 other，返回 current 自己的 22001；current 行必须保留
    assert crud.allocate_port(db, exclude_id=current_id) == 22001
    assert db.query(models.ContainerInstance).filter(models.ContainerInstance.id == current_id).first() is not None
    assert db.query(models.ContainerInstance).filter(models.ContainerInstance.id == other_id).first() is None


def test_allocate_raises_when_nothing_reclaimable(monkeypatch, db):
    monkeypatch.setattr(crud, "PORT_RANGE_START", 22000)
    monkeypatch.setattr(crud, "PORT_RANGE_END", 22001)   # 2 个端口
    monkeypatch.setattr(crud, "PORT_RECLAIM_THRESHOLD", 0)  # 关闭回收
    u = _u(db, "z")
    _mk(db, u, port=22000, cid="1" * 64)
    _mk(db, u, port=22001, cid="2" * 64)
    with pytest.raises(RuntimeError):
        crud.allocate_port(db)


def test_allocate_returns_first_free_without_reclaim_when_above_threshold(monkeypatch, db):
    """空闲足够时分配路径与旧行为一致：直接返回最小空闲，不做任何删除。"""
    monkeypatch.setattr(crud, "PORT_RANGE_START", 22000)
    monkeypatch.setattr(crud, "PORT_RANGE_END", 22009)   # 10 个端口
    monkeypatch.setattr(crud, "PORT_RECLAIM_THRESHOLD", 1)
    u = _u(db, "w")
    _mk(db, u, port=22002, cid="1" * 64)
    _mk(db, u, status="removed", port=22001, cid="2" * 64, days=3)
    n_before = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.status == "removed").count()
    assert crud.allocate_port(db) == 22000  # 最小空闲，不是 removed 行的 22001
    n_after = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.status == "removed").count()
    assert n_after == n_before
