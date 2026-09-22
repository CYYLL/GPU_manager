from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict

from ..database import get_db
from .. import schemas, models
from ..models import GpuAllocation
from ..services.gpu_monitor import get_gpu_monitor
from ..services.docker_runner import DockerRunner
from ..crud import containers as container_crud
from ..crud import users as user_crud
from ..auth import get_current_user, get_current_admin

router = APIRouter(tags=["gpus"])

gpu_monitor = get_gpu_monitor()
docker_runner = DockerRunner()

import os

# Threshold: GPU with <= this memory utilization is considered idle
STALE_MEMORY_THRESHOLD = int(os.getenv("STALE_MEMORY_THRESHOLD", "5"))  # percent


@router.get("/api/gpus/status", response_model=List[schemas.GPUStatus])
def get_gpu_status(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 1. Get DB-allocated GPU IDs
    allocated_ids = container_crud.get_allocated_gpu_ids(db)

    # 2. Build allocation details: gpu_id -> {username, container_instance_id}
    allocation_info: Dict[int, dict] = {}
    if allocated_ids:
        records = db.query(GpuAllocation).filter(
            GpuAllocation.gpu_id.in_(allocated_ids),
            GpuAllocation.released_at.is_(None)
        ).all()
        for rec in records:
            user = user_crud.get_user_by_id(db, rec.user_id)
            username = user.username if user else "unknown"
            # Get container instance for Docker status check
            inst = container_crud.get_container_instance(db, rec.container_instance_id)
            allocation_info[rec.gpu_id] = {
                "username": username,
                "container_id": inst.container_id if inst else None,
            }

    # 3. Get raw GPU status from NVML
    raw_statuses = gpu_monitor.get_gpu_status(
        allocated_gpu_ids=list(allocation_info.keys()),
        allocation_users={gid: info["username"] for gid, info in allocation_info.items()},
    )

    # 4. Override status with combined logic
    for gpu in raw_statuses:
        info = allocation_info.get(gpu["id"])
        container_id = info["container_id"] if info else None
        username = info["username"] if info else None

        gpu["allocated_to"] = username

        # Skip status override if GPU is in error state
        if gpu.get("error"):
            gpu["status"] = "error"
        elif not gpu["allocated"]:
            # No DB allocation — check if GPU is actually being used
            if gpu["memory_utilization"] > STALE_MEMORY_THRESHOLD or gpu["gpu_utilization"] > 10:
                gpu["status"] = "occupied"
            else:
                gpu["status"] = "free"
        else:
            # DB allocation exists — check Docker + memory
            container_running = False
            if container_id:
                container_running = docker_runner.is_container_running(container_id)

            memory_idle = gpu["memory_utilization"] <= STALE_MEMORY_THRESHOLD

            if container_running is None:
                gpu["status"] = "error"
                gpu["error"] = "无法确认容器 Docker 状态"
            elif container_running:
                gpu["status"] = "occupied"
            elif memory_idle:
                gpu["status"] = "stale"
            else:
                gpu["status"] = "occupied"

    return raw_statuses


@router.get("/api/admin/allocations", response_model=List[schemas.GPUAllocationOut])
def get_allocations(
    skip: int = 0,
    limit: int = 100,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    allocations = container_crud.get_allocations(db, skip=skip, limit=limit)
    result = []
    for a in allocations:
        user = user_crud.get_user_by_id(db, a.user_id)
        container = container_crud.get_container_instance(db, a.container_instance_id)
        result.append(schemas.GPUAllocationOut(
            id=a.id,
            gpu_id=a.gpu_id,
            username=user.username if user else "unknown",
            container_id=container.container_id[:12] if container else "unknown",
            allocated_at=a.allocated_at,
            released_at=a.released_at,
        ))
    return result
