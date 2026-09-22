"""Storage cleanup engine: candidate filter → constrained LLM choice →
guardrails → docker reclaim → capacity escalation.

Pure decision logic lives here and is unit-tested; docker/LLM/du are injected
(runner / llm_client parameters, module-level workspace_bytes_provider hook).
"""
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from ..crud import containers as crud
from ..services.docker_runner import DockerRunner

# module-level, replaced in tests
llm_client = None


# ── docker reclaim measurement (pure) ───────────────────────────────────────

def _docker_id(entry: dict) -> str:
    """Docker Engine uses Id; accept ID from older test/mocked payloads too."""
    return entry.get("Id") or entry.get("ID") or ""


def _is_stopped(entry: dict) -> bool:
    state = entry.get("State")
    if state is not None:
        return state in ("created", "exited", "dead")
    # Some callers supply the older boolean shape. Unknown state is not
    # counted as reclaimable, avoiding a false cleanup trigger.
    return entry.get("Running") is False


def _is_dangling(image: dict) -> bool:
    tags = image.get("RepoTags") or []
    return not tags or tags == ["<none>:<none>"]

def reclaim_breakdown(df: dict) -> Tuple[int, int, int]:
    """(stopped-container writable bytes, dangling-image bytes, build-cache bytes)
    from a `client.df()` payload. Running containers contribute nothing."""
    containers = df.get("Containers") or []
    images = df.get("Images") or []
    builds = df.get("BuildCache") or []
    ctr = sum(c.get("SizeRw") or 0 for c in containers if _is_stopped(c))
    img = sum(i.get("SizeRootFs") or i.get("Size") or 0 for i in images if _is_dangling(i))
    bc = sum(b.get("Size") or 0 for b in builds)
    return ctr, img, bc


def per_container_estimate(df: dict, container_id: str) -> int:
    """Source-level freed estimate for removing a container: its writable layer
    (SizeRw) plus the image rootfs IF that image dangles after removal (no other
    df container references it). Immune to concurrent /amax writes by design."""
    entry = None
    for c in df.get("Containers") or []:
        if _docker_id(c) == container_id or _docker_id(c).startswith(container_id[:12]):
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
        if _docker_id(i) == image_id and _is_dangling(i):
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


# ── control loop ─────────────────────────────────────────────────────────────

_GB = 1024 ** 3
_cleanup_lock_fd = None
_cleanup_thread_lock = threading.Lock()


def _lock_path() -> str:
    return os.environ.get("CLEANUP_LOCK_FILE", "/tmp/gpu_manager_v2_cleanup.lock")


def _acquire_global_lock() -> bool:
    """Thread lock + fcntl.flock (non-blocking). True when this process owns the
    round. Guarantees a single cleanup round runs at a time across threads AND
    across FastAPI worker processes."""
    if not _cleanup_thread_lock.acquire(blocking=False):
        return False
    try:
        import fcntl
    except ImportError:
        return True  # non-posix: process-level lock only
    fd = open(_lock_path(), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        _cleanup_thread_lock.release()
        return False
    globals()["_cleanup_lock_fd"] = fd
    return True


def _release_global_lock():
    fd = globals().pop("_cleanup_lock_fd", None)
    if fd is not None:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()
    if _cleanup_thread_lock.locked():
        _cleanup_thread_lock.release()


def _get_llm():
    global llm_client
    if llm_client is None:
        from .llm_client import create_llm_client
        llm_client = create_llm_client()
    return llm_client


def _mount_root() -> str:
    return os.environ.get("CONTAINER_MOUNT_ROOT", "/amax")


def _disk_usage_pct() -> float:
    usage = shutil.disk_usage(_mount_root())
    return usage.used / usage.total * 100


def _start_of_today() -> datetime:
    """UTC start of the current day (CleanupLog.ts is stored in UTC)."""
    now = datetime.utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def removed_today(db: Session) -> int:
    """Number of automatic removals logged since UTC start of today (daily cap)."""
    return db.query(models.CleanupLog).filter(
        models.CleanupLog.action == "remove",
        models.CleanupLog.ts >= _start_of_today()).count()


def _in_cooldown(db: Session, p: CleanupParams) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=p.cooldown_minutes)
    return db.query(models.CleanupLog).filter(models.CleanupLog.ts > cutoff).first() is not None


def _du_workspaces_bytes() -> int:
    """Total bytes of /amax/gpu-* workspace dirs. Heavy — only called on a
    suspected low-effect /amax round (design: never in the hourly sample)."""
    import glob
    import subprocess
    dirs = [d for d in glob.glob(os.path.join(_mount_root(), "gpu-*")) if os.path.isdir(d)]
    total = 0
    for d in dirs:
        try:
            r = subprocess.run(["du", "-sb", d], capture_output=True, text=True, timeout=120)
            total += int(r.stdout.split()[0])
        except Exception:
            continue
    return total


workspace_bytes_provider = _du_workspaces_bytes


def _log(db: Session, cid, uid, action, reason, decision_source, freed=0, success=True):
    db.add(models.CleanupLog(container_instance_id=cid, user_id=uid, action=action,
                             reason=reason, decision_source=decision_source,
                             freed_bytes=freed, success=success))
    db.commit()


def _llm_context(p: CleanupParams, usage_pct: float, ctr: int, img: int) -> str:
    return (f"当前磁盘使用 {usage_pct:.1f}%；容器层可回收约 {(ctr + img) // _GB}GB。"
            f"每轮最多清理 {p.max_per_round} 个容器。")


def run_cleanup_cycle(runner=None, db=None, llm_client=None) -> dict:
    """One guarded cleanup control cycle (called by the monitor thread).

    Pre-exits (disabled / lock busy / daily cap / cooldown / nothing to do),
    one round of ≤max_per_round removals with per-container TOCTOU re-check and
    source-level freed measurement, one image prune after the round, builder
    cache prune when independently due, and post-round effectiveness / capacity
    escalation. External calls (docker / du / LLM) never hold an open DB txn.
    """
    p = CleanupParams.from_env()
    if not p.enabled:
        return {"status": "disabled"}
    if runner is None:
        runner = DockerRunner()
    owns_db = db is None
    if owns_db:
        from ..database import SessionLocal
        db = SessionLocal()
    locked = False
    try:
        if not _acquire_global_lock():
            return {"status": "locked"}
        locked = True

        if removed_today(db) >= p.max_per_day:
            return {"status": "daily_cap"}
        if _in_cooldown(db, p):
            return {"status": "cooldown"}

        df = runner.df() or {}
        ctr, img, bc = reclaim_breakdown(df)
        usage_pct = _disk_usage_pct()
        usage0 = usage_pct

        # Builder cache maintenance: independent of container removal — prune
        # whenever the reclaimable build cache crosses its own threshold.
        build_pruned = 0
        if bc >= p.build_cache_trigger_gb * _GB and not p.dry_run:
            build_pruned = runner.build_cache_prune()

        active = usage_pct > p.disk_threshold or (ctr + img) >= p.container_reclaim_trigger_gb * _GB
        if not active:
            return {"status": "idle", "build_cache_pruned": build_pruned}

        auto, _alert = build_candidate_lists(db, df, p)
        if not auto:
            return {"status": "idle", "reason": "no stopped candidates",
                    "build_cache_pruned": build_pruned}

        target = p.disk_critical_target if usage_pct > p.disk_critical else p.disk_target
        llm = llm_client if llm_client is not None else _get_llm()
        features = decision_features(db, df, auto)
        sel = llm_select_candidates(llm, features, _llm_context(p, usage_pct, ctr, img), p)
        decision_source = "llm" if sel is not None else "rule_fallback"
        sel = (sel if sel is not None else select_by_rule(auto, p.max_per_round))[:p.max_per_round]

        generated_at = datetime.utcnow()
        removed = 0
        source_freed = 0
        image_pruned = 0
        for cid in sel:
            ok, reason = toctou_ok(db, cid, generated_at)
            if not ok:
                inst = crud.get_container_instance(db, cid)
                _log(db, cid, inst.user_id if inst else None, "skip", reason, decision_source)
                continue
            inst = crud.get_container_instance(db, cid)
            docker_running = runner.is_container_running(inst.container_id)
            if docker_running is not False:
                reason = "docker container running" if docker_running else "docker state unknown"
                _log(db, cid, inst.user_id, "skip", reason, decision_source)
                continue
            est = per_container_estimate(df, inst.container_id)  # source-level, pre-removal
            if p.dry_run:
                _log(db, cid, inst.user_id, "remove", "dry-run decision", decision_source,
                     freed=est, success=True)
                removed += 1
                continue
            mark_ok = crud.mark_container_removed(db, cid, runner, source="agent")
            _log(db, cid, inst.user_id, "remove", "", decision_source, freed=est, success=mark_ok)
            if mark_ok:
                removed += 1
                source_freed += est
            usage_pct = _disk_usage_pct()
            if usage_pct <= target:
                break  # target reached — stop the round

        if removed > 0 and not p.dry_run:
            image_pruned = runner.image_prune()   # once per round, never per container
            # image_pruned is NOT folded into source_freed: per_container_estimate
            # already attributes dangling-image layers to the removed container
            # (ImageID SizeRootFs when the image dangles), so adding it again here
            # would double count. Reported separately in the summary.

        escalated = False
        if removed > 0 and not p.dry_run:
            # Dry runs free nothing → never alert on (in)effectiveness.
            if usage0 <= p.disk_threshold:
                # Entered via the docker-reclaim trigger → noise-free effectiveness gate.
                eff, why = would_be_effective(source_freed, p, triggered_by_docker=True)
                if not eff:
                    level = "critical" if usage_pct > p.disk_critical else "warning"
                    raise_capacity_alert(
                        db,
                        f"清理回收低于有效阈值（源头测量 {source_freed} 字节），容器层无更多可回收空间",
                        level, {"source_freed": source_freed})
                    escalated = True
            elif usage_pct > p.disk_threshold:
                # /amax path still over threshold after the round → structural check.
                used_bytes = shutil.disk_usage(_mount_root()).used
                ws = workspace_bytes_provider()
                if used_bytes and ws / used_bytes > p.workspace_dominant_pct / 100.0:
                    level = "critical" if usage_pct > p.disk_critical else "warning"
                    raise_capacity_alert(
                        db,
                        "磁盘不足由用户工作空间占用导致（结构性），容器清理无法释放有效空间，"
                        "请管理员扩容或引导用户清理工作区",
                        level, {"workspace_bytes": ws})
                    escalated = True

        return {"status": "ok", "removed": removed, "decision_source": decision_source,
                "source_freed": source_freed, "image_pruned": image_pruned,
                "build_cache_pruned": build_pruned, "escalated": escalated}
    finally:
        if locked:
            _release_global_lock()
        if owns_db:
            db.close()
