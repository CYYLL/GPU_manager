import logging
import os
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from .. import models, schemas
from datetime import datetime

logger = logging.getLogger(__name__)


def get_image_by_id(db: Session, image_id: int):
    return db.query(models.GpuImage).filter(models.GpuImage.id == image_id).first()


def get_images(db: Session):
    return db.query(models.GpuImage).order_by(models.GpuImage.id).all()


def renumber_image_ids(db: Session):
    """Compact preset IDs after deletion; no table refers to GpuImage.id."""
    rows = get_images(db)
    moved = [(new_id, row) for new_id, row in enumerate(rows, start=1)
             if row.id != new_id]
    if not moved:
        return
    # Move through negative IDs so an existing row never occupies a target ID.
    for _, row in moved:
        row.id = -row.id
    db.flush()
    for new_id, row in moved:
        row.id = new_id
    db.flush()


def create_image(db: Session, image: schemas.GpuImageCreate, created_by: int):
    db_image = models.GpuImage(
        name=image.name,
        image=image.image,
        description=image.description,
        min_gpu=image.min_gpu,
        recommended_gpu=image.recommended_gpu,
        created_by=created_by,
    )
    db.add(db_image)
    db.commit()
    db.refresh(db_image)
    return db_image


def delete_image(db: Session, image_id: int) -> bool:
    db_image = db.query(models.GpuImage).filter(models.GpuImage.id == image_id).first()
    if not db_image:
        return False
    db.delete(db_image)
    db.flush()
    renumber_image_ids(db)
    db.commit()
    return True


def get_user_gpu_used(db: Session, user_id: int) -> int:
    """Count GPUs currently allocated to a user (not released)."""
    result = db.query(func.coalesce(func.count(models.GpuAllocation.id), 0)).filter(
        models.GpuAllocation.user_id == user_id,
        models.GpuAllocation.released_at.is_(None)
    ).scalar()
    return result or 0


def get_allocated_gpu_ids(db: Session) -> List[int]:
    """Return list of GPU IDs that are currently allocated (not released)."""
    records = db.query(models.GpuAllocation.gpu_id).filter(
        models.GpuAllocation.released_at.is_(None)
    ).all()
    return [r.gpu_id for r in records]


def create_allocations(db: Session, gpu_ids: List[int], container_instance_id: int, user_id: int):
    for gpu_id in gpu_ids:
        alloc = models.GpuAllocation(
            gpu_id=gpu_id,
            container_instance_id=container_instance_id,
            user_id=user_id,
        )
        db.add(alloc)
    db.commit()


def release_allocations_by_container(db: Session, container_instance_id: int):
    now = datetime.utcnow()
    db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == container_instance_id,
        models.GpuAllocation.released_at.is_(None)
    ).update({"released_at": now})
    db.commit()


def get_allocations(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.GpuAllocation).order_by(
        models.GpuAllocation.allocated_at.desc()
    ).offset(skip).limit(limit).all()


def create_container_instance(
    db: Session,
    user_id: int,
    container_id: str,
    image: str,
    gpu_ids: List[int],
    gpu_count: int,
    cpu_limit: Optional[float] = None,
    memory_limit: Optional[int] = None,
    assigned_port: Optional[int] = None,
    access_password: Optional[str] = None,
    ssh_username: Optional[str] = None,
    env_vars: Optional[dict] = None,
) -> models.ContainerInstance:
    now = datetime.utcnow()
    db_instance = models.ContainerInstance(
        user_id=user_id,
        container_id=container_id,
        image=image,
        gpu_ids=gpu_ids,
        gpu_count=gpu_count,
        status="running",
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        assigned_port=assigned_port,
        access_password=access_password,
        ssh_username=ssh_username,
        env_vars=env_vars or {},
        started_at=now,
    )
    db.add(db_instance)
    db.commit()
    db.refresh(db_instance)
    return db_instance


PORT_RANGE_START = int(os.getenv("PORT_RANGE_START", "22000"))
PORT_RANGE_END = int(os.getenv("PORT_RANGE_END", "22999"))
# 剩余空闲端口 ≤ 此值时自动回收最旧的 removed 快照来腾端口；0 = 关闭回收（保持纯 RuntimeError）。
PORT_RECLAIM_THRESHOLD = int(os.getenv("PORT_RECLAIM_THRESHOLD", "100"))


def _free_ports(db: Session, exclude_id: Optional[int] = None) -> List[int]:
    """空闲端口升序列表。

    可排除某个实例 id：rebuild 正在重建的那行既不能算占用它的旧端口，也不能被当作
    可回收的 removed 快照删除。
    """
    used_ports = set()
    q = db.query(models.ContainerInstance.assigned_port).filter(
        models.ContainerInstance.assigned_port.isnot(None)
    )
    if exclude_id is not None:
        q = q.filter(models.ContainerInstance.id != exclude_id)
    for (p,) in q.all():
        used_ports.add(p)
    return [p for p in range(PORT_RANGE_START, PORT_RANGE_END + 1) if p not in used_ports]


def allocate_port(db: Session, exclude_id: Optional[int] = None) -> int:
    """取 22000-22999 中最小未被占用的端口；端口池逼近上限时自动回收最旧的 removed 快照。

    返回顺序分配(最小空闲)。exclude_id 供 rebuild 排除正在重建的那行——既不计其占用，
    也不把它当作可回收的 removed 快照删除。回收复用 delete_container_instance（整行删除、
    连 GPU 分配一起清，语义与手动删除一致）；池里没有可回收快照而仍无空闲时抛 RuntimeError。
    """
    free = _free_ports(db, exclude_id)
    if PORT_RECLAIM_THRESHOLD > 0 and len(free) <= PORT_RECLAIM_THRESHOLD:
        _reclaim_removed_ports(db, exclude_id, target=PORT_RECLAIM_THRESHOLD)
        free = _free_ports(db, exclude_id)
    if not free:
        raise RuntimeError(f"No available ports in range {PORT_RANGE_START}-{PORT_RANGE_END}")
    return free[0]


def _reclaim_removed_ports(db: Session, exclude_id: Optional[int], target: int) -> int:
    """空闲数 ≤ target 时，按 stopped_at 从旧到新硬删 removed 快照，直到空闲 > target 或无可删。

    返回删除的行数；被回收的快照随之失去一键重建入口（有损，属预期保险行为）。
    """
    freed = 0
    while True:
        free = _free_ports(db, exclude_id)
        if len(free) > target:
            break
        cand = db.query(models.ContainerInstance).filter(
            models.ContainerInstance.status == "removed",
            models.ContainerInstance.assigned_port.isnot(None),
        )
        if exclude_id is not None:
            cand = cand.filter(models.ContainerInstance.id != exclude_id)
        row = cand.order_by(models.ContainerInstance.stopped_at.asc()).first()
        if row is None:
            break
        logger.warning("port pool low (%d free<=%d): reclaiming removed snapshot id=%s port=%s",
                       len(free), target, row.id, row.assigned_port)
        delete_container_instance(db, row.id)  # 整行删除+释放分配，语义与手动删除一致
        freed += 1
    return freed


def get_container_instance(db: Session, instance_id: int):
    return db.query(models.ContainerInstance).filter(
        models.ContainerInstance.id == instance_id
    ).first()


def get_container_instance_by_docker_id(db: Session, container_id: str):
    return db.query(models.ContainerInstance).filter(
        models.ContainerInstance.container_id == container_id
    ).first()


def get_user_containers(db: Session, user_id: int):
    return db.query(models.ContainerInstance).filter(
        models.ContainerInstance.user_id == user_id
    ).order_by(models.ContainerInstance.created_at.desc()).all()


def get_all_containers(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.ContainerInstance).order_by(
        models.ContainerInstance.created_at.desc()
    ).offset(skip).limit(limit).all()


def stop_container_instance(db: Session, instance_id: int):
    now = datetime.utcnow()
    db.query(models.ContainerInstance).filter(
        models.ContainerInstance.id == instance_id
    ).update({"status": "stopped", "stopped_at": now})
    db.commit()


def start_container_instance(db: Session, instance_id: int):
    """Mark a stopped container instance as running again."""
    now = datetime.utcnow()
    db.query(models.ContainerInstance).filter(
        models.ContainerInstance.id == instance_id
    ).update({"status": "running", "started_at": now, "stopped_at": None})
    db.commit()


def restore_container_instance(db: Session, instance: models.ContainerInstance) -> bool:
    """Re-mark a DB-stopped container instance as running because its Docker
    container is actually alive. Re-creates active GPU allocations for any of the
    instance's GPUs that aren't already claimed by another active container.

    Returns False (leaving state unchanged) if one of the instance's GPUs is
    claimed by a different active container -- a real conflict the caller must
    surface.
    """
    active = db.query(models.GpuAllocation).filter(
        models.GpuAllocation.released_at.is_(None)
    ).all()
    claimed = {a.gpu_id: a.container_instance_id for a in active}
    if any(claimed.get(gid) not in (None, instance.id) for gid in instance.gpu_ids):
        return False

    for gid in instance.gpu_ids:
        if gid not in claimed:
            db.add(models.GpuAllocation(
                gpu_id=gid,
                container_instance_id=instance.id,
                user_id=instance.user_id,
            ))
    start_container_instance(db, instance.id)
    return True


def update_container_gpus(db: Session, instance_id: int, new_docker_id: str, new_name: str, new_gpu_ids: List[int]):
    """Update container Docker ID and GPU assignment when restarting with different GPUs."""
    db.query(models.ContainerInstance).filter(
        models.ContainerInstance.id == instance_id
    ).update({
        "container_id": new_docker_id,
        "gpu_ids": new_gpu_ids,
    })
    db.commit()


def delete_container_instance(db: Session, instance_id: int) -> bool:
    """Delete a container instance and its GPU allocations."""
    instance = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.id == instance_id
    ).first()
    if not instance:
        return False
    # Delete allocations
    db.query(models.GpuAllocation).filter(
        models.GpuAllocation.container_instance_id == instance_id
    ).delete()
    # Delete instance
    db.delete(instance)
    db.commit()
    return True


def record_container_event(db: Session, instance_id: int, user_id: int,
                           event: str, source: str = "manual", detail: str = ""):
    """Record a lifecycle event. Commits independently (event write is its own
    short transaction — external calls never wrap DB work)."""
    db.add(models.ContainerEvent(
        container_instance_id=instance_id,
        user_id=user_id,
        event=event,
        source=source,
        detail=detail,
    ))
    db.commit()


def get_last_used(db: Session, instance_id: int):
    """Most recent ContainerEvent timestamp for an instance (cleanup LRU basis)."""
    ev = db.query(models.ContainerEvent).filter(
        models.ContainerEvent.container_instance_id == instance_id
    ).order_by(models.ContainerEvent.created_at.desc()).first()
    return ev.created_at if ev else None


def mark_container_removed(db: Session, instance_id: int, docker_runner,
                           source: str = "agent") -> bool:
    """Force-remove the docker container and mark the DB record `removed`
    (kept as the config snapshot for one-click rebuild, WITHOUT its port).
    Releases allocations and frees the external port so it re-enters the pool.

    Returns False — leaving DB state untouched, allocations intact — when the
    docker removal itself failed. A container that is still alive in docker must
    never be untracked nor have its GPUs released, or cleanup would claim a
    success it didn't achieve and risk double allocation.
    """
    inst = get_container_instance(db, instance_id)
    if inst is None:
        return False
    ok, _msg = docker_runner.remove_container(inst.container_id)
    if not ok:
        return False
    release_allocations_by_container(db, inst.id)
    inst.status = "removed"
    inst.stopped_at = datetime.utcnow()
    inst.assigned_port = None  # 释放对外端口：removed 快照不再占端口，重建时重新分配
    db.commit()
    db.refresh(inst)
    record_container_event(db, inst.id, inst.user_id, "delete", source,
                           detail="cleanup mark removed")
    return True
