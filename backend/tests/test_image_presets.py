from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models, schemas
from app.services import image_presets
from app.routers import containers as containers_router
from app.agent import tools as agent_tools
from app.crud import users as user_crud


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _user(db, name, role="user"):
    user = models.User(username=name, hashed_password="x", role=role)
    db.add(user)
    db.commit()
    return user


def _client():
    client = Mock()
    client.images.list.return_value = [
        SimpleNamespace(tags=["ubuntu:latest", "ubuntu:22.04"],
                        id="sha256:" + "a" * 64, attrs={"Size": 100}),
        SimpleNamespace(tags=["pytorch:2.5"],
                        id="sha256:" + "b" * 64, attrs={"Size": 200}),
        SimpleNamespace(tags=[], id="sha256:" + "c" * 64, attrs={"Size": 300}),
    ]
    return client


def test_users_choose_independent_presets_without_changing_local_images(monkeypatch, db):
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    client = _client()
    runner = SimpleNamespace(client=client)
    monkeypatch.setattr(containers_router, "docker_runner", runner)
    monkeypatch.setattr(agent_tools, "docker_runner", runner)

    available = containers_router.list_available_images(alice, db)
    assert len(available) == 4 and all(not row["selected"] for row in available)
    assert image_presets.sync_local_image_presets(db, client) == 0
    assert containers_router.list_images(alice, db) == []

    containers_router.update_image_preset(
        schemas.ImagePresetSelection(image_ref="pytorch:2.5", selected=True), alice, db)
    containers_router.update_image_preset(
        schemas.ImagePresetSelection(image_ref="ubuntu:latest", selected=True), bob, db)
    assert [row.image for row in containers_router.list_images(alice, db)] == ["pytorch:2.5"]
    assert [row.image for row in containers_router.list_images(bob, db)] == ["ubuntu:latest"]
    alice_listing = agent_tools.ToolExecutor(db, alice).run("list_local_images", {})[1]
    bob_listing = agent_tools.ToolExecutor(db, bob).run("list_local_images", {})[1]
    assert "image_ref=pytorch:2.5 image_id=sha256:bbbbbbbbbbbb size_bytes=200 preset_selected=true" in alice_listing
    assert "image_ref=pytorch:2.5 image_id=sha256:bbbbbbbbbbbb size_bytes=200 preset_selected=false" in bob_listing
    assert next(row for row in containers_router.list_available_images(alice, db)
                if row["image_ref"] == "pytorch:2.5")["selected"]

    image = containers_router.list_images(alice, db)[0]
    with pytest.raises(HTTPException) as error:
        containers_router._start_container_impl(
            schemas.ContainerStartRequest(image_id=image.id, gpu_count=1), bob, db)
    assert error.value.status_code == 403

    containers_router.update_image_preset(
        schemas.ImagePresetSelection(image_ref="pytorch:2.5", selected=False), alice, db)
    assert containers_router.list_images(alice, db) == []
    assert "image_ref=pytorch:2.5 image_id=sha256:bbbbbbbbbbbb size_bytes=200 preset_selected=false" in agent_tools.ToolExecutor(db, alice).run("list_local_images", {})[1]
    assert [row.image for row in containers_router.list_images(bob, db)] == ["ubuntu:latest"]
    client.images.remove.assert_not_called()


def test_legacy_migration_runs_once(monkeypatch, db):
    alice = _user(db, "alice")
    alice_id = alice.id
    db.add(models.GpuImage(name="Basic", image="basic:v1"))
    db.commit()
    migration_session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(image_presets, "SessionLocal", migration_session)

    image_presets.migrate_legacy_presets()
    assert [row.image for row in image_presets.selected_images(db, alice_id)] == ["basic:v1"]
    db.query(models.UserImagePreset).filter_by(user_id=alice_id).delete()
    db.commit()
    image_presets.migrate_legacy_presets()
    assert image_presets.selected_images(db, alice_id) == []
    bob = _user(db, "bob")
    assert image_presets.selected_images(db, bob.id) == []


def test_regular_user_can_select_local_image_with_llm_tool(monkeypatch, db):
    user = _user(db, "alice")
    client = _client()
    monkeypatch.setattr(agent_tools, "docker_runner", SimpleNamespace(client=client))
    executor = agent_tools.ToolExecutor(db, user)

    ok, message = executor.run("set_image_preset", {
        "image_ref": "pytorch:2.5", "selected": True})
    assert ok and "preset_selected=true" in message
    assert [row.image for row in image_presets.selected_images(db, user.id)] == ["pytorch:2.5"]
    ok, _ = executor.run("set_image_preset", {
        "image_ref": "missing:v1", "selected": True})
    assert not ok
    ok, _ = executor.run("pull_hub_image", {"image_ref": "pytorch:2.5"})
    assert not ok
    ok, _ = executor.run("delete_local_image", {"image_ref": "pytorch:2.5"})
    assert not ok


def test_only_admin_can_delete_real_image_and_all_selections_are_removed(monkeypatch, db):
    admin = _user(db, "admin", "admin")
    alice = _user(db, "alice")
    client = _client()
    client.images.get.return_value = Mock(id="sha256:" + "b" * 64)
    client.containers.list.return_value = []
    monkeypatch.setattr(containers_router, "docker_runner", SimpleNamespace(client=client))
    image_presets.sync_local_image_presets(db, client)
    for user in (admin, alice):
        image_presets.set_selection(db, user.id, "pytorch:2.5", True, client)
    image = db.query(models.GpuImage).filter_by(image="pytorch:2.5").first()

    containers_router.admin_delete_local_image(image.id, admin, db)

    assert db.query(models.UserImagePreset).filter_by(image_ref="pytorch:2.5").count() == 0
    assert db.query(models.GpuImage).filter_by(image="pytorch:2.5").count() == 0
    client.images.remove.assert_called_once_with("pytorch:2.5", force=False)


def test_deleting_user_preserves_other_users_preset(db):
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    image = models.GpuImage(name="Shared", image="shared:v1", created_by=alice.id)
    db.add(image)
    db.add(models.UserImagePreset(user_id=alice.id, image_ref="shared:v1"))
    db.add(models.UserImagePreset(user_id=bob.id, image_ref="shared:v1"))
    db.commit()

    assert user_crud.delete_user(db, alice.id)

    surviving = db.query(models.User).filter_by(username="bob").one()
    assert [row.image for row in image_presets.selected_images(db, surviving.id)] == ["shared:v1"]
    assert db.query(models.GpuImage).filter_by(image="shared:v1").one().created_by is None
