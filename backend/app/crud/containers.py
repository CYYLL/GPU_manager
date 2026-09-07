import os
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from .. import models, schemas
from datetime import datetime


def get_image_by_id(db: Session, image_id: int):
    return db.query(models.GpuImage).filter(models.GpuImage.id == image_id).first()


def get_images(db: Session):
    return db.query(models.GpuImage).all()


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
        env_vars=env_vars or {},
        started_at=now,
    )
    db.add(db_instance)
    db.commit()
    db.refresh(db_instance)
    return db_instance


PORT_RANGE_START = int(os.getenv("PORT_RANGE_START", "22000"))
PORT_RANGE_END = int(os.getenv("PORT_RANGE_END", "22999"))


def allocate_port(db: Session) -> int:
    """Find the first available port in 22000-22999 that is not assigned to any container."""
    used_ports = set()
    results = db.query(models.ContainerInstance.assigned_port).filter(
        models.ContainerInstance.assigned_port.isnot(None)
    ).all()
    for (p,) in results:
        used_ports.add(p)
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in used_ports:
            return port
    raise RuntimeError(f"No available ports in range {PORT_RANGE_START}-{PORT_RANGE_END}")


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
    (kept as the config snapshot for one-click rebuild). Releases allocations.

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
    db.commit()
    db.refresh(inst)
    record_container_event(db, inst.id, inst.user_id, "delete", source,
                           detail="cleanup mark removed")
    return True
