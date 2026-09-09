"""Admin quota tools: set_user_quota (modify) + check_gpu_quota(username) (query).

Both are admin-only for targeting another user; enforcement is at the handler
(the toolset stays fixed for everyone; a normal user is refused, never silently
allowed). Values/delegate mirror the REST admin endpoint /api/admin/users/{id}/quota
(crud.update_user_quota, 0 <= gpu_quota <= 4).
"""
from unittest import mock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import tools as agent_tools


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


def _mk_user(db, name, role="user", quota=2):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=quota)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_used(db, user, gpu_id=0):
    """Give a user one live GPU allocation (counts toward get_user_gpu_used)."""
    inst = models.ContainerInstance(
        user_id=user.id, container_id=("c" * 64), image="basic:v1",
        gpu_ids=[gpu_id], gpu_count=1, status="running",
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    a = models.GpuAllocation(gpu_id=gpu_id, container_instance_id=inst.id, user_id=user.id)
    db.add(a)
    db.commit()


# ── catalog / schema shape ───────────────────────────────────────────────────

def test_quota_tools_present_and_shape():
    by_name = {t["name"]: t for t in agent_tools.TOOLS}
    assert "set_user_quota" in by_name
    s = by_name["set_user_quota"]
    assert set(s) == {"name", "description", "input_schema"}  # 只含网关三键
    assert s["input_schema"]["required"] == ["username", "gpu_quota"]
    c = by_name["check_gpu_quota"]
    assert "username" in c["input_schema"]["properties"]  # 可选管理员查询参数


# ── admin query another user's quota (check_gpu_quota + username) ────────────

def test_admin_queries_other_user_quota(db):
    admin = _mk_user(db, "root", role="admin")
    u = _mk_user(db, "pat", quota=2)
    _mk_used(db, u, gpu_id=0)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("check_gpu_quota", {"username": "pat"})
    assert ok is True
    assert "用户 pat" in text and "配额=2" in text and "已用=1" in text and "剩余=1" in text


def test_admin_query_with_gpu_count_fits_and_not(db):
    admin = _mk_user(db, "root", role="admin")
    _mk_user(db, "pat", quota=2)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("check_gpu_quota", {"username": "pat", "gpu_count": 2})
    assert ok is True and "可以" in text
    ok, text = ex.run("check_gpu_quota", {"username": "pat", "gpu_count": 3})
    assert ok is True and "不可以" in text


def test_admin_query_case_insensitive_username(db):
    admin = _mk_user(db, "root", role="admin")
    _mk_user(db, "Pat", quota=1)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("check_gpu_quota", {"username": "pat"})
    assert ok is True and "用户 Pat" in text


def test_admin_query_unknown_user(db):
    admin = _mk_user(db, "root", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("check_gpu_quota", {"username": "ghost"})
    assert ok is False and "用户不存在" in text


def test_normal_user_cannot_query_other(db):
    u = _mk_user(db, "owen")
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("check_gpu_quota", {"username": "owen"})
    assert ok is False and "仅管理员" in text


# ── set_user_quota (modify, admin-only) ──────────────────────────────────────

def test_admin_sets_user_quota(db):
    admin = _mk_user(db, "root", role="admin")
    u = _mk_user(db, "pat", quota=1)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("set_user_quota", {"username": "pat", "gpu_quota": 4})
    assert ok is True
    assert "设为 4" in text
    db.refresh(u)
    assert u.gpu_quota == 4


def test_set_user_quota_rejects_bounds_and_types(db):
    admin = _mk_user(db, "root", role="admin")
    u = _mk_user(db, "pat", quota=2)
    ex = agent_tools.ToolExecutor(db, admin)
    for bad in ({"username": "pat", "gpu_quota": 5},
                {"username": "pat", "gpu_quota": -1},
                {"username": "pat", "gpu_quota": "abc"}):
        ok, text = ex.run("set_user_quota", bad)
        assert ok is False
    db.refresh(u)
    assert u.gpu_quota == 2  # 失败不落库


def test_set_user_quota_unknown_user(db):
    admin = _mk_user(db, "root", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("set_user_quota", {"username": "ghost", "gpu_quota": 2})
    assert ok is False and "用户不存在" in text


def test_set_user_quota_refuses_admin_target(db):
    admin = _mk_user(db, "root", role="admin")
    other_admin = _mk_user(db, "boss", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("set_user_quota", {"username": "boss", "gpu_quota": 4})
    assert ok is False and "无需配额" in text
    db.refresh(other_admin)
    assert other_admin.gpu_quota != 4  # 未被改动


def test_normal_user_cannot_set_quota(db):
    u = _mk_user(db, "owen")
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("set_user_quota", {"username": "owen", "gpu_quota": 3})
    assert ok is False and "仅管理员" in text
    db.refresh(u)
    assert u.gpu_quota == 2  # 未受影响


def test_set_user_quota_missing_args(db):
    admin = _mk_user(db, "root", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("set_user_quota", {"gpu_quota": 2})
    assert ok is False and "username" in text


# ── list_users（枚举普通用户，admin-only；覆盖无容器用户）───────────────────────

def test_list_users_present_and_shape():
    by_name = {t["name"]: t for t in agent_tools.TOOLS}
    assert "list_users" in by_name
    s = by_name["list_users"]
    assert set(s) == {"name", "description", "input_schema"}  # 只含网关三键
    assert s["input_schema"]["properties"] == {}


def test_admin_lists_users_including_containerless(db):
    admin = _mk_user(db, "root", role="admin")
    _mk_user(db, "testuser1", quota=1)   # 无容器：必须仍出现在列表（否则只能间接推断）
    pat = _mk_user(db, "pat", quota=2)
    _mk_used(db, pat, gpu_id=0)          # pat：1 卡占用 + 1 running 容器
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("list_users", {})
    assert ok is True
    assert "testuser1" in text and "gpu_quota=1" in text and "gpu_used=0" in text
    assert "pat" in text and "gpu_quota=2" in text and "gpu_used=1" in text
    assert "containers=1" in text and "running=1" in text
    assert "username=root" not in text  # 管理员不出现在普通用户列表


def test_list_users_excludes_admins(db):
    admin = _mk_user(db, "root", role="admin")
    _mk_user(db, "boss", role="admin")
    _mk_user(db, "onlyme", quota=2)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("list_users", {})
    assert ok is True
    assert "root" not in text and "boss" not in text and "onlyme" in text


def test_list_users_empty_when_no_regular_users(db):
    admin = _mk_user(db, "root", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("list_users", {})
    assert ok is True and "没有普通用户" in text


def test_normal_user_cannot_list_users(db):
    u = _mk_user(db, "owen")
    _mk_user(db, "someone", quota=2)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("list_users", {})
    assert ok is False and "仅管理员" in text


# ── delete_user（管理员删除普通用户；同 User management 页删除语义）──────────────

def test_delete_user_present_and_shape():
    by_name = {t["name"]: t for t in agent_tools.TOOLS}
    assert "delete_user" in by_name
    s = by_name["delete_user"]
    assert set(s) == {"name", "description", "input_schema"}  # 只含网关三键
    assert s["input_schema"]["required"] == ["username"]


def test_admin_deletes_user_clears_containers_and_allocations(monkeypatch, db):
    """删除：先 stop/remove 该用户名下 docker 容器，再清容器记录 + GPU 分配 + 账号。"""
    admin = _mk_user(db, "root", role="admin")
    pat = _mk_user(db, "pat", quota=2)
    pat_id = pat.id  # commit/删除后 ORM 过期 → 事后只按捕获的整数查询
    _mk_used(db, pat, gpu_id=0)   # 1 running 容器 + 1 活动 GPU 分配
    cid = "c" * 64
    db.query(models.ContainerInstance).filter(
        models.ContainerInstance.user_id == pat_id
    ).update({"container_id": cid})
    db.commit()

    stub = mock.Mock()
    stub.stop_container.return_value = (True, "stopped")
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("delete_user", {"username": "pat"})

    assert ok is True
    assert "已删除用户 pat" in text
    assert "工作区目录已保留" in text  # 宿主机工作区不删的契约（无 fs 代码路径）
    # docker 层：该容器被 stop + remove 各一次
    stub.stop_container.assert_called_once_with(cid)
    stub.remove_container.assert_called_once_with(cid)
    # DB 层：账号 / 容器记录 / GPU 分配全部清空
    assert db.query(models.User).filter(models.User.username == "pat").first() is None
    assert db.query(models.ContainerInstance).filter(
        models.ContainerInstance.container_id == cid).first() is None
    assert db.query(models.GpuAllocation).filter(
        models.GpuAllocation.user_id == pat_id).count() == 0


def test_admin_delete_user_renumbers_remaining_users(monkeypatch, db):
    """删除中间用户后，剩余用户 id 与 FK 引用（容器 user_id）被重排为连续。"""
    admin = _mk_user(db, "root", role="admin")   # id=1
    a = _mk_user(db, "user_a", quota=2)          # id=2，将被删除
    b = _mk_user(db, "user_b", quota=2)          # id=3，删除后应变成 id=2
    inst_a = models.ContainerInstance(
        user_id=a.id, container_id="a" * 64, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status="running", assigned_port=22000)
    inst_b = models.ContainerInstance(
        user_id=b.id, container_id="b" * 64, image="basic:v1",
        gpu_ids=[1], gpu_count=1, status="running", assigned_port=22001)
    db.add_all([inst_a, inst_b])
    db.commit()
    db.refresh(inst_a)
    db.refresh(inst_b)
    b_id_before, inst_b_id = b.id, inst_b.id  # commit 会过期对象 → 先取整数

    stub = mock.Mock()
    stub.stop_container.return_value = (True, "stopped")
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("delete_user", {"username": "user_a"})

    assert ok is True and "已删除用户 user_a" in text
    assert db.query(models.User).filter(models.User.username == "user_a").first() is None
    # user_b 前移占位，其容器 FK 一并指向新 id
    b_now = db.query(models.User).filter(models.User.username == "user_b").first()
    assert b_now is not None
    assert b_now.id == b_id_before - 1
    inst_b_now = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.id == inst_b_id).first()
    assert inst_b_now is not None
    assert inst_b_now.user_id == b_now.id


def test_admin_delete_user_refuses_admin_target(db):
    admin = _mk_user(db, "root", role="admin")
    other_admin = _mk_user(db, "boss", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("delete_user", {"username": "boss"})
    assert ok is False and "管理员账号" in text
    assert db.query(models.User).filter(models.User.username == "boss").first() is not None  # 未删


def test_admin_delete_user_unknown_user(db):
    admin = _mk_user(db, "root", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("delete_user", {"username": "ghost"})
    assert ok is False and "用户不存在" in text


def test_admin_delete_user_missing_args(db):
    admin = _mk_user(db, "root", role="admin")
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("delete_user", {})
    assert ok is False and "username" in text


def test_normal_user_cannot_delete_user(db):
    u = _mk_user(db, "owen")
    _mk_user(db, "victim", quota=2)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("delete_user", {"username": "victim"})
    assert ok is False and "仅管理员" in text
    assert db.query(models.User).filter(models.User.username == "victim").first() is not None  # 未删
