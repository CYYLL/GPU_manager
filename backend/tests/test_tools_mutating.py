"""Mutating tools delegate to lifecycle handlers; ownership + errors mapped."""
from unittest import mock
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import tools as agent_tools
from app import schemas
from pydantic import ValidationError


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


def _mk_user(db, name="vince"):
    u = models.User(username=name, hashed_password="x", role="user", gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_inst(db, user, status="running", cid="9" * 64):
    inst = models.ContainerInstance(
        user_id=user.id, container_id=cid, image="basic:v1",
        gpu_ids=[0], gpu_count=1, status=status,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def test_stop_container_tool(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk_inst(db, u)
    # patch the router module's docker_runner (handlers use it as a module global)
    from app.routers import containers as containers_router
    stub = mock.Mock()
    stub.stop_container.return_value = (True, "stopped")
    stub.is_container_running.return_value = False
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("stop_container", {"id": inst.id})
    assert ok is True
    assert "stopped" in text.lower() or "已" in text
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.source == "llm"


def test_stop_container_denies_other_owner(monkeypatch, db):
    u = _mk_user(db)
    other = _mk_user(db, "wade")
    inst = _mk_inst(db, other, cid="8" * 64)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("stop_container", {"id": inst.id})
    assert ok is False
    assert "not authorized" in text.lower() or "未授权" in text or "not running" in text.lower()


def test_delete_container_tool(monkeypatch, db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, cid="7" * 64)
    from app.routers import containers as containers_router
    stub = mock.Mock()
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("delete_container", {"id": inst.id})
    assert ok is True


def test_tools_list_has_mutating_schemas():
    names = {t["name"] for t in agent_tools.TOOLS}
    assert {"create_container", "start_container", "stop_container", "delete_container", "list_images"} <= names
    by_name = {t["name"]: t for t in agent_tools.TOOLS}
    assert by_name["stop_container"]["input_schema"]["required"] == ["id"]
    assert by_name["create_container"]["input_schema"]["required"] == ["image_id"]


@pytest.mark.parametrize("message,payload", [
    ("创建一个 0 张 GPU 的容器", {"image_id": 1}),
    ("创建一个 0 张 GPU 的容器", {"image_id": 1, "gpu_count": 1}),
    ("创建一个 2 张 GPU 的容器", {"image_id": 1, "gpu_count": 1}),
    ("创建一个 1 张 GPU 的容器", {"image_id": 1, "gpu_count": 1.5}),
    ("创建一个 5 张 GPU 的容器", {"image_id": 1}),
])
def test_create_tool_never_substitutes_gpu_count(monkeypatch, db, message, payload):
    from app.routers import containers as containers_router
    start = mock.Mock()
    monkeypatch.setattr(containers_router, "_start_container_impl", start)
    ex = agent_tools.ToolExecutor(db, _mk_user(db))
    ex.user_message = message

    ok, result = ex.run("create_container", payload)

    assert not ok and "未创建容器" in result
    start.assert_not_called()


@pytest.mark.parametrize("message,payload,expected", [
    ("创建一个容器", {"image_id": 1}, 1),
    ("创建一个容器", {"image_id": 1, "gpu_count": 2}, 1),
    ("创建一个 2 张 GPU 的容器", {"image_id": 1}, 2),
    ("创建一个 2 张 GPU 的容器", {"image_id": 1, "gpu_count": 2}, 2),
    ("创建一个两张GPU的容器", {"image_id": 1}, 2),
])
def test_create_tool_uses_default_or_explicit_gpu_count(monkeypatch, db, message, payload, expected):
    from app.routers import containers as containers_router
    start = mock.Mock(return_value=mock.Mock(
        id=3, image="basic:v1", status="running", ssh_username="root", assigned_port=None))
    monkeypatch.setattr(containers_router, "_start_container_impl", start)
    ex = agent_tools.ToolExecutor(db, _mk_user(db))
    ex.user_message = message

    ok, result = ex.run("create_container", payload)

    assert ok and "容器已创建" in result
    assert start.call_args.args[0].gpu_count == expected


def test_container_request_defaults_to_one_and_rejects_zero():
    assert schemas.ContainerStartRequest(image_id=1).gpu_count == 1
    with pytest.raises(ValidationError):
        schemas.ContainerStartRequest(image_id=1, gpu_count=0)


def test_create_tool_reports_image_minimum_only_when_validation_fails(monkeypatch, db):
    from app.routers import containers as containers_router
    start = mock.Mock(side_effect=HTTPException(
        status_code=400, detail="镜像预设最低需要 2 张 GPU，当前请求 1 张"))
    monkeypatch.setattr(containers_router, "_start_container_impl", start)
    ex = agent_tools.ToolExecutor(db, _mk_user(db))
    ex.user_message = "创建一个容器"

    ok, result = ex.run("create_container", {"image_id": 1})

    assert not ok and "最低需要 2 张 GPU" in result and "未创建容器" in result
    assert start.call_args.args[0].gpu_count == 1


def test_llm_repairs_container_ssh_and_records_username(monkeypatch, db):
    from app.routers import containers as containers_router
    owner = _mk_user(db)
    instance = _mk_inst(db, owner)
    instance.access_password = "secret"
    db.commit()
    runner = mock.Mock()
    runner.is_container_running.return_value = True
    runner.setup_ssh.return_value = (True, "gpuuser", "SSH 登录已就绪")
    monkeypatch.setattr(containers_router, "docker_runner", runner)

    ok, result = agent_tools.ToolExecutor(db, owner).run(
        "repair_container_ssh", {"id": instance.id})

    assert ok and "ssh_user=gpuuser" in result
    assert db.get(models.ContainerInstance, instance.id).ssh_username == "gpuuser"
    runner.setup_ssh.assert_called_once_with(instance.container_id, "secret", None)


def test_other_user_cannot_repair_container_ssh(monkeypatch, db):
    from app.routers import containers as containers_router
    owner = _mk_user(db)
    other = _mk_user(db, "other")
    instance = _mk_inst(db, owner)
    runner = mock.Mock()
    monkeypatch.setattr(containers_router, "docker_runner", runner)

    ok, result = agent_tools.ToolExecutor(db, other).run(
        "repair_container_ssh", {"id": instance.id})

    assert not ok and "无权" in result
    runner.setup_ssh.assert_not_called()


# ── 管理员只允许 stop/delete：create/start/rebuild 一律拒绝 ──────────────────

def _mk_admin(db):
    u = models.User(username="root", hashed_password="x", role="admin", gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_admin_create_container_refused(db):
    admin = _mk_admin(db)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("create_container", {})
    assert ok is False
    assert "不能创建" in text and "只能停止和删除" in text


def test_admin_start_container_refused(db):
    admin = _mk_admin(db)
    u = _mk_user(db)
    inst = _mk_inst(db, u, status="stopped", cid="6" * 64)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("start_container", {"id": inst.id})
    assert ok is False and "只能停止和删除" in text


def test_admin_rebuild_container_refused(db):
    admin = _mk_admin(db)
    u = _mk_user(db)
    inst = _mk_inst(db, u, status="removed", cid="5" * 64)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("rebuild_container", {"id": inst.id})
    assert ok is False and "只能停止和删除" in text


def test_admin_stop_user_container_allowed(monkeypatch, db):
    admin = _mk_admin(db)
    u = _mk_user(db)
    inst = _mk_inst(db, u, cid="4" * 64)
    from app.routers import containers as containers_router
    stub = mock.Mock()
    stub.stop_container.return_value = (True, "stopped")
    stub.is_container_running.return_value = False
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("stop_container", {"id": inst.id})
    assert ok is True and "stopped" in text.lower() or "已" in text
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.source == "llm"


def test_admin_delete_user_container_allowed(monkeypatch, db):
    admin = _mk_admin(db)
    u = _mk_user(db)
    inst = _mk_inst(db, u, cid="3" * 64)
    from app.routers import containers as containers_router
    stub = mock.Mock()
    stub.remove_container.return_value = (True, "removed")
    monkeypatch.setattr(containers_router, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("delete_container", {"id": inst.id})
    assert ok is True


def test_create_container_output_does_not_leak_password(monkeypatch, db, tmp_path):
    from app.routers import containers as containers_router
    u = _mk_user(db)
    im = models.GpuImage(name="pytorch:latest", image="docker.io/library/pytorch:latest", min_gpu=1)
    db.add(im)
    db.commit()
    db.refresh(im)
    db.add(models.UserImagePreset(user_id=u.id, image_ref=im.image))
    db.commit()

    # isolated workspace mount root so the impl never writes to the real /amax tree
    monkeypatch.setenv("CONTAINER_MOUNT_ROOT", str(tmp_path))
    # docker_runner.start_container returns a fake docker id + running status
    stub = mock.Mock()
    stub.start_container.return_value = ("d" * 64, "running", "root")
    monkeypatch.setattr(containers_router, "docker_runner", stub)
    # idle GPUs: availability + busy-GPU checks pass for a single GPU
    monkeypatch.setattr(containers_router.gpu_monitor, "get_gpu_count", lambda: 1)
    monkeypatch.setattr(
        containers_router.gpu_monitor, "get_gpu_status",
        lambda *a, **k: [{"id": 0, "memory_utilization": 0.0, "gpu_utilization": 0}],
    )

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("create_container", {"image_id": im.id, "gpu_count": 1})
    assert ok is True
    inst = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.user_id == u.id
    ).first()
    assert inst is not None
    assert f"id={inst.id}" in text
    assert f"port={inst.assigned_port}" in text
    assert inst.access_password  # DB 仍保存，用户去容器列表页取
    # 敏感信息防护：agent 输出/聊天历史里不得出现明文访问密码。
    assert f"password={inst.access_password}" not in text
    assert "password=" not in text
    assert "访问密码" in text and "容器列表" in text  # 引导去列表页复制，不在此展示
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == inst.id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    assert ev.event == "create" and ev.source == "llm"
