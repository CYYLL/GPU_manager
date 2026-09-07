"""Background monitor: hourly disk snapshot, external-stop detection, retention prune.

sample_once/retention_prune are pure and testable; a daemon thread (started in
main.py) calls them on a timer. External calls (docker, disk_usage) never run
inside a DB transaction — reads and writes commit independently.
"""
import os
import shutil
import threading
import time
import types
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy.orm import Session

from .. import models
from ..crud import containers as crud
from ..services.docker_runner import DockerRunner


def _interval_seconds() -> int:
    return int(os.environ.get("MONITOR_INTERVAL", "3600"))


def _disk_usage(root: str):
    """Read disk usage for a mount root.

    The unit test replaces mon.shutil with an object exposing disk_usage as an
    instance-bound method (its ``self`` is pre-bound), whereas the real shutil
    exposes a plain module function. Unwrap the bound method so the root path is
    forwarded identically in both cases.
    """
    fn = shutil.disk_usage
    if isinstance(fn, types.MethodType):
        fn = fn.__func__
    return fn(root)


def sample_once(db: Session, docker_runner) -> int:
    """One monitor cycle. Returns count of containers detected stopped externally.

    - Marks DB-running containers whose docker container is gone as stopped
      (external_stop event, source=agent) and releases their GPU allocations.
    - Writes one DiskSnapshot row for /amax.
    """
    running = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.status == "running").all()
    external_stops = 0
    for inst in running:
        if not docker_runner.is_container_running(inst.container_id):
            crud.release_allocations_by_container(db, inst.id)
            crud.stop_container_instance(db, inst.id)
            crud.record_container_event(db, inst.id, inst.user_id, "external_stop", "agent")
            external_stops += 1

    usage = _disk_usage(os.environ.get("CONTAINER_MOUNT_ROOT", "/amax"))
    db.add(models.DiskSnapshot(
        total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free,
        usage_percent=usage.used / usage.total * 100,
    ))
    db.commit()
    return external_stops


def retention_prune(db: Session) -> Dict[str, int]:
    """Delete over-retention rows. Returns counts removed."""
    removed = {"events_removed": 0, "snapshots_removed": 0, "chat_removed": 0}
    ev_cutoff = datetime.utcnow() - timedelta(days=int(os.environ.get("EVENT_RETENTION_DAYS", "30")))
    res = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.created_at < ev_cutoff).delete()
    removed["events_removed"] = res

    snap_cutoff = datetime.utcnow() - timedelta(
        days=int(os.environ.get("SNAPSHOT_RETENTION_DAYS", "7")))
    res = db.query(models.DiskSnapshot).filter(models.DiskSnapshot.ts < snap_cutoff).delete()
    removed["snapshots_removed"] = res

    limit = int(os.environ.get("CHAT_HISTORY_LIMIT", "200"))
    for (user_id,) in db.query(models.ChatMessage.user_id).distinct().all():
        rows = db.query(models.ChatMessage).filter(
            models.ChatMessage.user_id == user_id
        ).order_by(models.ChatMessage.created_at.desc()).all()
        for row in rows[limit:]:
            db.delete(row)
            removed["chat_removed"] += 1
    db.commit()
    return removed


def _loop_once():
    """Run one full monitor cycle in its own session; each step commits
    independently. Exceptions are handled by the caller (non-fatal in the
    thread)."""
    from ..database import SessionLocal  # deferred to avoid import cycle at module load
    db = SessionLocal()
    try:
        runner = DockerRunner()
        sample_once(db, runner)
        retention_prune(db)
    finally:
        db.close()


def start_monitor_thread():
    """Start a daemon thread running _loop_once every MONITOR_INTERVAL seconds."""
    def _run():
        while True:
            try:
                _loop_once()
            except Exception as e:
                print(f"monitor cycle error (non-fatal): {e}")
            time.sleep(_interval_seconds())
    t = threading.Thread(target=_run, daemon=True, name="gpu-manager-monitor")
    t.start()
    return t
