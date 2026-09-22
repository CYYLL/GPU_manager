"""Restore SSH access when a running managed container loses its sshd process."""

import logging
import os
import threading
import time

from .. import models
from ..crud import containers as container_crud
from ..services.docker_runner import DockerRunner
from ..services.ssh_access import ssh_banner_ready

logger = logging.getLogger(__name__)


def check_running_ssh(db, runner) -> int:
    """Repair unreachable SSH for running containers; leave Docker state alone."""
    instances = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.status == "running",
        models.ContainerInstance.access_password.isnot(None),
        models.ContainerInstance.assigned_port.isnot(None),
    ).all()
    repaired = 0
    for instance in instances:
        if runner.is_container_running(instance.container_id) is not True:
            continue
        if ssh_banner_ready(instance.assigned_port):
            continue
        ok, username, reason = runner.ensure_ssh(
            instance.container_id, instance.access_password, instance.ssh_username)
        if not ok:
            logger.warning("SSH repair failed for container %s: %s", instance.id, reason)
            continue
        instance.ssh_username = username
        db.commit()
        container_crud.record_container_event(
            db, instance.id, instance.user_id, "ssh_repair", "agent")
        repaired += 1
        logger.info("SSH restored for container %s", instance.id)
    return repaired


def start_ssh_watchdog_thread():
    """Check at startup and periodically after sshd crashes or external restarts."""
    interval = max(10, int(os.environ.get("SSH_WATCHDOG_INTERVAL_SECONDS", "60")))

    def _run():
        from ..database import SessionLocal

        while True:
            db = SessionLocal()
            try:
                check_running_ssh(db, DockerRunner())
            except Exception:
                logger.exception("SSH watchdog cycle failed")
            finally:
                db.close()
            time.sleep(interval)

    thread = threading.Thread(target=_run, daemon=True, name="gpu-manager-ssh-watchdog")
    thread.start()
    return thread
