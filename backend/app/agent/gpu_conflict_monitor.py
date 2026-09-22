"""Report overlapping GPU device access without stopping running containers."""

import logging
import os
import threading
import time
from datetime import datetime

from .. import models
from ..services.docker_runner import DockerRunner
from ..services.gpu_monitor import get_gpu_monitor

logger = logging.getLogger(__name__)


def check_gpu_conflicts(db, runner, total_gpu_count: int):
    """Upsert one alert for current Docker/DB ownership conflicts."""
    claims = runner.get_running_gpu_claims(total_gpu_count)
    active = db.query(models.GpuAllocation, models.ContainerInstance).join(
        models.ContainerInstance,
        models.GpuAllocation.container_instance_id == models.ContainerInstance.id,
    ).filter(models.GpuAllocation.released_at.is_(None)).all()
    db_owners = {}
    for allocation, instance in active:
        db_owners.setdefault(allocation.gpu_id, {})[instance.container_id] = instance.id

    conflicts = []
    for gpu_id in sorted(set(claims) | set(db_owners)):
        docker_containers = {entry["container_id"]: entry for entry in claims.get(gpu_id, [])}
        owner_ids = set(docker_containers) | set(db_owners.get(gpu_id, {}))
        if len(owner_ids) < 2:
            continue
        conflicts.append({
            "gpu_id": gpu_id,
            "containers": [{
                "container_id": container_id[:12],
                "name": docker_containers.get(container_id, {}).get("name", "数据库登记容器"),
                "instance_id": db_owners.get(gpu_id, {}).get(container_id),
            } for container_id in sorted(owner_ids)],
        })

    alert = db.query(models.AdminAlert).filter(
        models.AdminAlert.type == "gpu_conflict",
        models.AdminAlert.resolved_at.is_(None),
    ).first()
    if conflicts:
        descriptions = [
            f"GPU {entry['gpu_id']}: " + ", ".join(
                f"{container['name']}({container['container_id']})"
                for container in entry["containers"])
            for entry in conflicts
        ]
        message = ("；".join(descriptions)
                   + "。GPU 访问范围重叠，请核对容器归属；系统不会自动停止容器")
        if alert is None:
            db.add(models.AdminAlert(
                type="gpu_conflict", level="warning", message=message,
                meta={"conflicts": conflicts}))
        else:
            alert.message = message
            alert.meta = {"conflicts": conflicts}
        db.commit()
    elif alert is not None:
        alert.resolved_at = datetime.utcnow()
        db.commit()
    return conflicts


def start_gpu_conflict_thread():
    interval = max(10, int(os.environ.get("GPU_CONFLICT_INTERVAL_SECONDS", "60")))

    def _run():
        from ..database import SessionLocal

        while True:
            db = SessionLocal()
            try:
                check_gpu_conflicts(db, DockerRunner(), get_gpu_monitor().get_gpu_count())
            except Exception:
                logger.exception("GPU conflict check failed")
            finally:
                db.close()
            time.sleep(interval)

    thread = threading.Thread(target=_run, daemon=True, name="gpu-manager-gpu-conflicts")
    thread.start()
    return thread
