"""Storage cleanup engine: candidate filter → constrained LLM choice →
guardrails → docker reclaim → capacity escalation.

Pure decision logic lives here and is unit-tested; docker/LLM/du are injected
(runner / llm_client parameters, module-level workspace_bytes_provider hook).
"""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from ..crud import containers as crud


# ── docker reclaim measurement (pure) ───────────────────────────────────────

def reclaim_breakdown(df: dict) -> Tuple[int, int, int]:
    """(stopped-container writable bytes, dangling-image bytes, build-cache bytes)
    from a `client.df()` payload. Running containers contribute nothing."""
    containers = df.get("Containers") or []
    images = df.get("Images") or []
    builds = df.get("BuildCache") or []
    ctr = sum(c.get("SizeRw") or 0 for c in containers if not c.get("Running"))
    img = sum(i.get("SizeRootFs") or i.get("Size") or 0 for i in images if not i.get("RepoTags"))
    bc = sum(b.get("Size") or 0 for b in builds)
    return ctr, img, bc


def per_container_estimate(df: dict, container_id: str) -> int:
    """Source-level freed estimate for removing a container: its writable layer
    (SizeRw) plus the image rootfs IF that image dangles after removal (no other
    df container references it). Immune to concurrent /amax writes by design."""
    entry = None
    for c in df.get("Containers") or []:
        if c.get("ID") == container_id or c.get("ID", "").startswith(container_id[:12]):
            entry = c
            break
    if entry is None:
        return 0
    rw = entry.get("SizeRw") or 0
    image_id = entry.get("ImageID")
    if not image_id:
        return rw
    others = [c for c in (df.get("Containers") or []) if c is not entry and c.get("ImageID") == image_id]
    if others:
        return rw  # image still in use elsewhere
    for i in df.get("Images") or []:
        if i.get("ID") == image_id:
            return rw + (i.get("SizeRootFs") or i.get("Size") or 0)
    return rw


# ── parameters (env-driven, read fresh each control cycle) ─────────────────

@dataclass
class CleanupParams:
    enabled: bool = True
    dry_run: bool = False
    disk_threshold: float = 85.0
    disk_critical: float = 95.0
    disk_target: float = 80.0
    disk_critical_target: float = 90.0
    cooldown_minutes: int = 5
    max_per_day: int = 10
    max_per_round: int = 3
    container_reclaim_trigger_gb: int = 20
    build_cache_trigger_gb: int = 100
    min_effective_free_gb: int = 5
    workspace_dominant_pct: float = 60.0
    grace_days: int = 2

    @classmethod
    def from_env(cls) -> "CleanupParams":
        e = os.environ.get
        gb = 1024 ** 3
        return cls(
            enabled=e("CLEANUP_ENABLED", "true").lower() not in ("0", "false", "no"),
            dry_run=e("CLEANUP_DRY_RUN", "false").lower() in ("1", "true", "yes"),
            disk_threshold=float(e("DISK_THRESHOLD", "85")),
            disk_critical=float(e("DISK_CRITICAL", "95")),
            disk_target=float(e("DISK_TARGET", "80")),
            disk_critical_target=float(e("DISK_CRITICAL_TARGET", "90")),
            cooldown_minutes=int(e("CLEANUP_COOLDOWN_MINUTES", "5")),
            max_per_day=int(e("CLEANUP_MAX_PER_DAY", "10")),
            max_per_round=int(e("CLEANUP_MAX_PER_ROUND", "3")),
            container_reclaim_trigger_gb=int(e("CONTAINER_RECLAIM_TRIGGER_GB", "20")),
            build_cache_trigger_gb=int(e("BUILD_CACHE_TRIGGER_GB", "100")),
            min_effective_free_gb=int(e("MIN_EFFECTIVE_FREE_GB", "5")),
            workspace_dominant_pct=float(e("WORKSPACE_DOMINANT_PCT", "60")),
            grace_days=int(e("GRACE_DAYS", "2")),
        )


# ── candidate filtering (deterministic, no LLM) ────────────────────────────

def build_candidate_lists(db: Session, df: dict, p: CleanupParams):
    """LRU-ordered candidate lists. Returns (auto, alert): auto = stopped &
    unprotected & past grace; alert = running & unprotected (never auto-stopped).
    Protected containers appear in neither."""
    cutoff = datetime.utcnow() - timedelta(days=p.grace_days)
    insts = crud.get_all_containers(db)

    def _used_recently(i) -> bool:
        lu = crud.get_last_used(db, i.id)
        return lu is None or lu >= cutoff  # never-used treated as recent (safe)

    auto, alert = [], []
    for i in insts:
        if i.cleanup_protected or i.status in ("removed", "error"):
            continue
        if _used_recently(i):
            continue
        entry = {"id": i.id, "status": i.status, "container_id": i.container_id,
                 "image": i.image, "user_id": i.user_id, "protected": i.cleanup_protected}
        if i.status == "running":
            alert.append(entry)
        elif i.status == "stopped":
            auto.append(entry)

    def _lu(entry):
        return crud.get_last_used(db, entry["id"]) or datetime.min

    auto.sort(key=_lu)    # LRU ascending → oldest unused first
    alert.sort(key=_lu)
    return auto, alert


def decision_features(db: Session, df: dict, entries: List[dict]) -> List[dict]:
    """Deterministic per-candidate features for the constrained LLM choice."""
    out = []
    for e in entries:
        uid = e["user_id"]
        n_user = db.query(models.ContainerInstance).filter(
            models.ContainerInstance.user_id == uid).count()
        prev = db.query(models.CleanupLog).filter(
            models.CleanupLog.container_instance_id == e["id"]).count()
        est = per_container_estimate(df, e["container_id"])
        out.append({
            "id": e["id"], "last_used": str(crud.get_last_used(db, e["id"])),
            "reclaim_bytes": est, "user_container_count": n_user,
            "prev_cleanup_count": prev,
            "image": e["image"], "status": e["status"],
        })
    return out


def select_by_rule(entries: List[dict], k: int) -> List[int]:
    """Rule fallback: the first k candidates in LRU order."""
    return [e["id"] for e in entries[:k]]


def validate_selection(selected, allowed_ids) -> bool:
    return all(s in allowed_ids for s in selected)


def llm_select_candidates(llm_client, features, context: str, p: CleanupParams) -> Optional[List[int]]:
    """Ask LLM to pick ⊆ candidates. Returns ids or None (no decision → rule fallback).
    Any id outside the candidate set rejects the whole round (anti-hallucination)."""
    allowed = {f["id"] for f in features}
    if not allowed:
        return None
    tool = {"name": "select_cleanup_candidates",
            "description": "从给出的候选容器中选出本轮应删除的容器（自动清理仅限已停止容器）",
            "input_schema": {"type": "object",
                             "properties": {"ids": {"type": "array",
                                                    "items": {"type": "integer"},
                                                    "description": "候选 id 的子集"},
                                            "reasons": {"type": "object"}},
                             "required": ["ids", "reasons"]}}
    cand_rows = "\n".join(
        f"- id={f['id']} last_used={f.get('last_used')} reclaim_bytes={f.get('reclaim_bytes', 0)} "
        f"user_containers={f.get('user_container_count', 0)} prev_cleanup={f.get('prev_cleanup_count', 0)} "
        f"image={f.get('image', '')} status={f.get('status', '')}" for f in features)
    prompt = (f"{context}\n候选容器（只能从其中选择，最多 {p.max_per_round} 个，"
              f"只选 stopped）：\n{cand_rows}")
    try:
        res = llm_client.complete(
            "你是存储清理决策器。只删除给出候选中的、已停止且最该清理的容器。"
            "权衡：重建成本低、回收收益高、用户容器多者优先；避免反复清理同一容器。",
            [{"role": "user", "content": prompt}], [tool])
    except Exception:
        return None
    for call in res.tool_calls or []:
        ids = (call.get("input") or {}).get("ids") or []
        if validate_selection(ids, allowed):
            return ids
    return None


# ── TOCTOU re-validation + effectiveness + alerts ───────────────────────────

def toctou_ok(db: Session, instance_id: int, generated_at: datetime) -> Tuple[bool, str]:
    """A candidate may only be acted on if nothing (start/create/rebuild) happened
    after candidate generation and the docker container is not running."""
    inst = crud.get_container_instance(db, instance_id)
    if inst is None or inst.status != "stopped":
        return False, "state changed since candidate selection"
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == instance_id,
        models.ContainerEvent.created_at > generated_at).first()
    if ev is not None:
        return False, "container state changed since candidate selection"
    return True, ""


def would_be_effective(source_freed: int, p: CleanupParams, triggered_by_docker: bool) -> Tuple[bool, str]:
    if source_freed < p.min_effective_free_gb * 1024 ** 3:
        return False, f"low effective reclaim ({source_freed} bytes)"
    return True, ""


def raise_capacity_alert(db: Session, message: str, level: str, meta: dict):
    """Idempotent: do not stack unresolved capacity alerts."""
    open_alert = db.query(models.AdminAlert).filter(
        models.AdminAlert.type == "capacity",
        models.AdminAlert.resolved_at.is_(None)).first()
    if open_alert is not None:
        return
    db.add(models.AdminAlert(type="capacity", level=level, message=message, meta=meta))
    db.commit()
