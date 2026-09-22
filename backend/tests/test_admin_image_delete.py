from unittest import mock

import pytest
from docker.errors import APIError
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.agent import tools as agent_tools
from app.routers import containers as containers_router


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _image(db, ref="pytorch/pytorch:2.5"):
    row = models.GpuImage(name="PyTorch", image=ref, min_gpu=1, recommended_gpu=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _runner(monkeypatch, used_by):
    client = mock.Mock()
    client.images.get.return_value = mock.Mock(id="sha256:" + "a" * 64)
    client.containers.list.return_value = used_by
    monkeypatch.setattr(containers_router, "docker_runner", mock.Mock(client=client))
    return client


def test_used_image_is_rejected_and_preset_kept(monkeypatch, db):
    image = _image(db)
    client = _runner(monkeypatch, [mock.Mock(id="container1")])
    with pytest.raises(HTTPException) as err:
        containers_router._delete_local_image_impl(image.id, db)
    assert err.value.status_code == 409
    assert "正被" in err.value.detail and "拒绝" in err.value.detail
    client.images.remove.assert_not_called()
    assert db.query(models.GpuImage).count() == 1


def test_unused_image_removes_local_tag_and_duplicate_presets(monkeypatch, db):
    image = _image(db)
    image_ref = image.image
    image_id = image.id
    _image(db, image_ref)
    client = _runner(monkeypatch, [])
    result = containers_router._delete_local_image_impl(image_id, db)
    client.containers.list.assert_called_once_with(
        all=True, filters={"ancestor": "sha256:" + "a" * 64})
    client.images.remove.assert_called_once_with(image_ref, force=False)
    assert "已删除" in result["message"]
    assert db.query(models.GpuImage).count() == 0


def test_deleting_local_image_compacts_remaining_preset_ids(monkeypatch, db):
    first = _image(db, "first:v1")
    middle = _image(db, "middle:v1")
    last = _image(db, "last:v1")
    _runner(monkeypatch, [])

    containers_router._delete_local_image_impl(middle.id, db)

    assert [(row.id, row.image) for row in db.query(models.GpuImage).order_by(models.GpuImage.id)] == [
        (1, "first:v1"), (2, "last:v1"),
    ]


def test_normal_user_cannot_call_delete_tool(monkeypatch, db):
    image = _image(db)
    user = models.User(username="normal", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    executor = agent_tools.ToolExecutor(db, user)
    executor.user_message = "删除 " + image.image
    ok, result = executor.run("delete_local_image", {"image_id": image.id})
    assert not ok and "仅管理员" in result


def test_docker_conflict_after_check_keeps_preset(monkeypatch, db):
    image = _image(db)
    client = _runner(monkeypatch, [])
    client.images.remove.side_effect = APIError("image is used by container")
    with pytest.raises(HTTPException) as err:
        containers_router._delete_local_image_impl(image.id, db)
    assert err.value.status_code == 409
    assert db.query(models.GpuImage).count() == 1
