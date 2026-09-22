"""Read-only + protection tools: ownership enforced, live queries, structured output."""
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import tools as agent_tools


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


def _mk_user(db, name="owen", role="user"):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=8)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_inst(db, user, status="running", protected=False, container_id=None):
    inst = models.ContainerInstance(
        user_id=user.id,
        container_id=container_id or ("a" * 64),
        image="basic:v1", gpu_ids=[0], gpu_count=1,
        status=status, cleanup_protected=protected,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _mk_image(db, name="pytorch:latest"):
    im = models.GpuImage(name=name, image=f"docker.io/library/{name}", min_gpu=1)
    db.add(im)
    db.commit()
    db.refresh(im)
    return im


def test_list_images(db):
    u = _mk_user(db)
    im = _mk_image(db)
    db.add(models.UserImagePreset(user_id=u.id, image_ref=im.image))
    db.commit()
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("list_images", {})
    assert ok is True
    assert f"id={im.id}" in text
    assert im.name in text
    assert "min_gpu" in text


def test_list_images_empty(db):
    u = _mk_user(db)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("list_images", {})
    assert ok is True
    assert "没有可用镜像" in text


def test_hub_search_requires_local_image_check(monkeypatch, db):
    admin = _mk_user(db)
    admin.role = "admin"
    executor = agent_tools.ToolExecutor(db, admin)
    searches = []
    monkeypatch.setattr(agent_tools, "search_images", lambda user_id, query: searches.append(query) or [])

    ok, result = executor.run("search_hub_images", {"query": "pytorch"})
    assert not ok and "list_local_images" in result
    assert searches == []

    # The preset table alone is incomplete and must not unlock Hub search.
    assert executor.run("list_images", {})[0]
    assert not executor.run("search_hub_images", {"query": "pytorch"})[0]
    monkeypatch.setattr(agent_tools, "docker_runner", SimpleNamespace(client=SimpleNamespace(
        images=SimpleNamespace(list=lambda: []))))
    assert executor.run("list_local_images", {})[0]
    ok, result = executor.run("search_hub_images", {"query": "pytorch"})
    assert ok and searches == ["pytorch"]


def test_all_users_can_list_and_inspect_unregistered_local_image(monkeypatch, db):
    user = _mk_user(db)
    local = SimpleNamespace(tags=["private/app:v1"], id="sha256:" + "a" * 64,
                            attrs={"Size": 1234})
    monkeypatch.setattr(agent_tools, "docker_runner", SimpleNamespace(client=SimpleNamespace(
        images=SimpleNamespace(list=lambda: [local]))))
    monkeypatch.setattr(agent_tools, "inspect_local_image", lambda ref: {"image": ref})
    executor = agent_tools.ToolExecutor(db, user)

    ok, listing = executor.run("list_local_images", {})
    assert ok and "private/app:v1" in listing and "size_bytes=1234" in listing
    assert "preset_selected=false" in listing
    assert db.query(models.GpuImage).count() == 0
    ok, report = executor.run("inspect_image", {"image_ref": "private/app:v1"})
    assert ok and '"image": "private/app:v1"' in report


def test_all_users_can_inspect_registered_image(monkeypatch, db):
    user = _mk_user(db)
    image = _mk_image(db)
    monkeypatch.setattr(agent_tools, "inspect_local_image", lambda ref: {
        "image": ref, "base_os": "Ubuntu 22.04", "notable_packages": ["python3=3.10"]})
    executor = agent_tools.ToolExecutor(db, user)
    ok, result = executor.run("inspect_image", {"image_id": image.id})
    assert ok and "Ubuntu 22.04" in result and "python3=3.10" in result
    ok, result = executor.run("inspect_image", {"image_id": image.id + 100})
    assert not ok and "不存在" in result


def test_list_containers_only_own(monkeypatch, db):
    u = _mk_user(db)
    _mk_inst(db, u)
    other = _mk_user(db, "pat")
    _mk_inst(db, other, container_id="b" * 64)
    # docker_runner imported at module scope; point it at a stub
    stub = type("DockerStub", (), {"is_container_running": lambda self, cid: True})()
    monkeypatch.setattr(agent_tools, "docker_runner", stub)
    monkeypatch.setattr(agent_tools, "get_host_ip", lambda: "10.0.0.5")
    own = db.query(models.ContainerInstance).filter_by(user_id=u.id).first()
    own.assigned_port = 22013
    db.commit()

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("list_containers", {})
    assert ok is True
    assert text.count("a" * 12) >= 1 and text.count("b" * 12) == 0
    assert "host_ip=10.0.0.5 port=22013" in text


def test_access_address_without_ip(monkeypatch):
    monkeypatch.setattr(agent_tools, "get_host_ip", lambda: None)
    assert agent_tools._access_address(22013) == " host_ip=未知 port=22013"


def test_admin_list_sees_all_users_containers(monkeypatch, db):
    admin = _mk_user(db, "root", role="admin")
    _mk_inst(db, admin, container_id="a" * 64)
    other = _mk_user(db, "pat")
    _mk_inst(db, other, container_id="b" * 64)
    stub = type("DockerStub", (), {"is_container_running": lambda self, cid: True})()
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("list_containers", {})
    assert ok is True
    assert text.count("a" * 12) >= 1
    assert text.count("b" * 12) >= 1
    assert "user=root" in text and "user=pat" in text


def test_admin_get_status_allows_other_owner(monkeypatch, db):
    admin = _mk_user(db, "root", role="admin")
    other = _mk_user(db, "quin")
    inst = _mk_inst(db, other, container_id="c" * 64)
    stub = type("DockerStub", (), {"is_container_running": lambda self, cid: True})()
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("get_container_status", {"id": inst.id})
    assert ok is True
    assert "user=quin" in text


def test_get_status_denies_other_owner(monkeypatch, db):
    u = _mk_user(db)
    other = _mk_user(db, "quin")
    inst = _mk_inst(db, other, container_id="c" * 64)
    stub = type("DockerStub", (), {"is_container_running": lambda self, cid: True})()
    monkeypatch.setattr(agent_tools, "docker_runner", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("get_container_status", {"id": inst.id})
    assert ok is False
    assert "not authorized" in text.lower() or "未授权" in text


def test_get_gpu_status_unreadable(monkeypatch, db):
    u = _mk_user(db)
    stub = type("GpuMonitorStub", (),
                {"get_gpu_status":
                 lambda self, allocated_gpu_ids=None, allocation_users=None: []})()
    monkeypatch.setattr(agent_tools, "gpu_monitor", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("get_gpu_status", {})
    assert ok is True
    assert "无法读取" in text


def test_get_gpu_status_reports_owner_per_card(monkeypatch, db):
    # 与 GPU 看板一致：普通用户也能看到被占卡的占用者用户名（不输容器等更细信息）
    u = _mk_user(db, "owen")
    other = _mk_user(db, "alice")
    inst_alice = _mk_inst(db, other, container_id="a" * 64)   # gpu0 -> alice
    inst_owen = _mk_inst(db, u, container_id="b" * 64)        # gpu1 -> owen
    db.add(models.GpuAllocation(gpu_id=0, container_instance_id=inst_alice.id, user_id=other.id))
    db.add(models.GpuAllocation(gpu_id=1, container_instance_id=inst_owen.id, user_id=u.id))
    db.commit()
    stub = type("GpuMonitorStub", (),
                {"get_gpu_status":
                 lambda self, allocated_gpu_ids=None, allocation_users=None: [
                     {"id": 0, "name": "NVIDIA RTX 3090", "memory_utilization": 31.0,
                      "gpu_utilization": 78, "allocated": True, "allocated_to": "alice"},
                     {"id": 1, "name": "NVIDIA RTX 3090", "memory_utilization": 2.0,
                      "gpu_utilization": 0, "allocated": True, "allocated_to": "owen"},
                     {"id": 2, "name": "NVIDIA RTX 3090", "memory_utilization": 0.0,
                      "gpu_utilization": 0, "allocated": False, "allocated_to": None},
                     {"id": 3, "name": "NVIDIA RTX 3090", "memory_utilization": 90.0,
                      "gpu_utilization": 95, "allocated": False, "allocated_to": None},
                 ]})()
    monkeypatch.setattr(agent_tools, "gpu_monitor", stub)

    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("get_gpu_status", {})
    assert ok is True
    assert "gpu=0" in text and "user=alice" in text   # 已登记占用者
    assert "gpu=1" in text and "user=owen" in text
    assert "gpu=2" in text and "free" in text          # 空闲卡
    assert "gpu=3" in text and "busy(未登记)" in text   # NVML 有负载但无归属
    # 不暴露容器短 id（选择"仅用户名"粒度）
    assert ("a" * 12) not in text and ("b" * 12) not in text


def test_unknown_tool(db):
    u = _mk_user(db)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("nope", {})
    assert ok is False
    assert "Unknown tool" in text


def _mk_user_quota(db, name="owen", role="user", quota=2):
    u = models.User(username=name, hashed_password="x", role=role, gpu_quota=quota)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_alloc(db, user, gpu_id=0):
    inst = _mk_inst(db, user)
    a = models.GpuAllocation(
        gpu_id=gpu_id, container_instance_id=inst.id, user_id=user.id)
    db.add(a)
    db.commit()


def test_check_gpu_quota_reports_remaining(db):
    u = _mk_user_quota(db, quota=2)
    _mk_alloc(db, u, gpu_id=0)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("check_gpu_quota", {})
    assert ok is True
    assert "配额=2" in text and "已用=1" in text and "剩余=1" in text


def test_check_gpu_quota_request_fits_and_not(db):
    u = _mk_user_quota(db, quota=2)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("check_gpu_quota", {"gpu_count": 2})
    assert ok is True and "可以" in text
    ok, text = ex.run("check_gpu_quota", {"gpu_count": 3})
    assert ok is True and "不可以" in text


def test_check_gpu_quota_admin_unlimited(db):
    admin = _mk_user_quota(db, "root", role="admin", quota=0)
    ex = agent_tools.ToolExecutor(db, admin)
    ok, text = ex.run("check_gpu_quota", {"gpu_count": 4})
    assert ok is True and "无 GPU 配额限制" in text


def test_set_protection_persists(db):
    u = _mk_user(db)
    inst = _mk_inst(db, u, protected=False)
    ex = agent_tools.ToolExecutor(db, u)
    ok, text = ex.run("set_container_protection", {"id": inst.id, "protected": True})
    assert ok is True
    db.refresh(inst)
    assert inst.cleanup_protected is True
