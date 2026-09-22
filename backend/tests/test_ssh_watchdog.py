from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.agent import ssh_watchdog
from app.database import Base


def test_watchdog_repairs_running_container_with_closed_ssh_port(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        user = models.User(username="watchdog-user", hashed_password="x", role="user", gpu_quota=1)
        db.add(user)
        db.flush()
        instance = models.ContainerInstance(
            user_id=user.id, container_id="a" * 64, image="basic:v1", gpu_ids=[0],
            gpu_count=1, status="running", assigned_port=22000,
            access_password="secret", ssh_username="root",
        )
        db.add(instance)
        db.commit()
        runner = mock.Mock()
        runner.is_container_running.return_value = True
        runner.ensure_ssh.return_value = (True, "gpuuser", "SSH 登录已就绪")
        monkeypatch.setattr(ssh_watchdog, "ssh_banner_ready", lambda port: False)

        assert ssh_watchdog.check_running_ssh(db, runner) == 1
        runner.ensure_ssh.assert_called_once_with("a" * 64, "secret", "root")
        db.refresh(instance)
        assert instance.ssh_username == "gpuuser"
        assert db.query(models.ContainerEvent).filter_by(
            container_instance_id=instance.id, event="ssh_repair", source="agent").count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(engine)
