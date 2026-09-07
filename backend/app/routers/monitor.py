"""Monitor/admin data routes: on-demand container status + disk trend + cleanup
candidates/logs + capacity alerts. Mode-guarded per design (traditional → 403).

Authorization is enforced in-body (not only via Depends) so the route functions
behave identically under FastAPI DI and direct unit-test calls.
"""
import os
import shutil
from datetime import datetime
from typing import List, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..database import get_db
from .. import models, schemas
from ..auth import get_current_user, get_current_admin
from .mode import require_llm_mode
from ..crud import containers as container_crud
from ..services.docker_runner import DockerRunner
from ..agent.cleanup import CleanupParams, build_candidate_lists, decision_features

router = APIRouter(tags=["monitor"])
docker_runner = DockerRunner()


def _admin_only(user: models.User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")


def _live_usage():
    u = shutil.disk_usage(os.environ.get("CONTAINER_MOUNT_ROOT", "/amax"))
    return u.total, u.used, u.free, u.used / u.total * 100


@router.get("/api/monitor/containers")
def containers(current_user: models.User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    require_llm_mode(current_user)
    insts = (container_crud.get_all_containers(db) if current_user.role == "admin"
             else container_crud.get_user_containers(db, current_user.id))
    out = []
    for i in insts:
        out.append({"id": i.id, "container_id": i.container_id[:12], "image": i.image,
                    "status": i.status,
                    "docker_running": docker_runner.is_container_running(i.container_id),
                    "gpu_ids": i.gpu_ids, "gpu_count": i.gpu_count,
                    "assigned_port": i.assigned_port,
                    "cleanup_protected": i.cleanup_protected})
    return out


@router.get("/api/monitor/disk")
def disk_status(current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    require_llm_mode(current_user)
    latest = db.query(models.DiskSnapshot).order_by(models.DiskSnapshot.ts.desc()).first()
    trend = db.query(models.DiskSnapshot).order_by(models.DiskSnapshot.ts.desc()).limit(24).all()
    t, used, free, pct = _live_usage()
    return {"latest": ({"ts": latest.ts, "usage_percent": latest.usage_percent,
                        "free_bytes": latest.free_bytes,
                        "docker_container_reclaimable_bytes": latest.docker_container_reclaimable_bytes,
                        "docker_image_reclaimable_bytes": latest.docker_image_reclaimable_bytes,
                        "docker_build_cache_reclaimable_bytes": latest.docker_build_cache_reclaimable_bytes}
                       if latest else None),
            "trend": [{"ts": s.ts, "usage_percent": s.usage_percent, "free_bytes": s.free_bytes}
                      for s in reversed(trend)],
            "live": {"total_bytes": t, "used_bytes": used, "free_bytes": free,
                     "usage_percent": round(pct, 1)}}


@router.get("/api/monitor/candidates")
def candidates(current_user: models.User = Depends(get_current_admin),
               db: Session = Depends(get_db)):
    require_llm_mode(current_user)
    _admin_only(current_user)
    p = CleanupParams.from_env()
    auto, alert = build_candidate_lists(db, {}, p)  # {} df → features w/o docker sizes
    df = docker_runner.df()
    return {"auto": decision_features(db, df, auto),
            "alert": decision_features(db, df, alert),
            "params": {"grace_days": p.grace_days, "dry_run": p.dry_run}}


@router.get("/api/admin/cleanup-log")
def cleanup_log(current_user: models.User = Depends(get_current_admin),
                db: Session = Depends(get_db)):
    _admin_only(current_user)
    rows = db.query(models.CleanupLog).order_by(models.CleanupLog.ts.desc()).limit(200).all()
    return [{"id": r.id, "ts": r.ts, "container_instance_id": r.container_instance_id,
             "user_id": r.user_id, "action": r.action, "reason": r.reason,
             "decision_source": r.decision_source, "freed_bytes": r.freed_bytes,
             "success": r.success} for r in rows]


@router.get("/api/admin/alerts")
def alerts(current_user: models.User = Depends(get_current_admin),
           db: Session = Depends(get_db)):
    _admin_only(current_user)
    rows = db.query(models.AdminAlert).order_by(
        models.AdminAlert.resolved_at.is_(None).desc(), models.AdminAlert.ts.desc()).all()
    return [{"id": a.id, "ts": a.ts, "type": a.type, "level": a.level,
             "message": a.message, "meta": a.meta, "resolved_at": a.resolved_at} for a in rows]


@router.post("/api/admin/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int,
                  current_user: models.User = Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    _admin_only(current_user)
    a = db.query(models.AdminAlert).filter(models.AdminAlert.id == alert_id).first()
    if a is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "resolved"}
