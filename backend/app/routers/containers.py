from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List
import uuid
import secrets
import string
import os
from docker.types import Mount

from ..database import get_db
from .. import schemas, models
from ..crud import containers as container_crud
from ..crud import users as user_crud
from ..services.gpu_allocator import GPUAllocator
from ..services.docker_runner import DockerRunner
from ..services.gpu_monitor import get_gpu_monitor
from ..auth import get_current_user, get_current_admin

router = APIRouter(tags=["containers"])

gpu_monitor = get_gpu_monitor()
docker_runner = DockerRunner()


@router.get("/api/images", response_model=List[schemas.GpuImageOut])
def list_images(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return container_crud.get_images(db)


@router.post("/api/containers/start", response_model=schemas.ContainerResponse)
def start_container(
    req: schemas.ContainerStartRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 1. Validate image exists
    image_record = container_crud.get_image_by_id(db, req.image_id)
    if not image_record:
        raise HTTPException(status_code=404, detail="Image not found")

    # 2. Validate GPU count >= image minimum
    if req.gpu_count < image_record.min_gpu:
        raise HTTPException(
            status_code=400,
            detail=f"This image requires at least {image_record.min_gpu} GPU(s)",
        )

    # 3. Check user does not already have a running container
    existing_containers = container_crud.get_user_containers(db, current_user.id)
    running_containers = [c for c in existing_containers if c.status == "running"]
    if len(running_containers) > 0:
        raise HTTPException(
            status_code=400,
            detail="You already have a running container. Please stop it before starting a new one.",
        )

    allocator = GPUAllocator(total_gpu_count=gpu_monitor.get_gpu_count())

    # Acquire global allocation lock to prevent TOCTOU race conditions.
    # This serializes all concurrent allocation requests so that checking
    # GPU availability and reserving them happens atomically.
    try:
        with allocator.allocate_guard(timeout=int(os.environ.get("ALLOCATION_TIMEOUT", "60"))):
            # 4. Check quota
            quota_ok, quota_msg = allocator.check_quota(db, current_user.id, req.gpu_count, current_user.role)
            if not quota_ok:
                raise HTTPException(status_code=400, detail=quota_msg)

            # 4. Find available GPUs
            # Get NVML status to identify GPUs with active usage (no DB record but in use)
            raw_statuses = gpu_monitor.get_gpu_status()
            allocated_ids = container_crud.get_allocated_gpu_ids(db)
            busy_ids = [
                g["id"] for g in raw_statuses
                if g["id"] not in allocated_ids and (
                    g.get("memory_utilization", 0) > 5 or g.get("gpu_utilization", 0) > 10
                )
            ]
            available = allocator.find_available_gpus(db, req.gpu_count, busy_gpu_ids=busy_ids)
            if len(available) < req.gpu_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"Not enough GPUs. Requested: {req.gpu_count}, Available: {len(available)}",
                )

            gpu_ids = available[:req.gpu_count]

            # 5. Allocate host port and generate access password
            assigned_port = container_crud.allocate_port(db)
            access_password = ''.join(secrets.choice(string.ascii_letters + string.digits + "-_") for _ in range(8))

            # 6. Start Docker container with port mapping and volume mount
            container_name = f"gpu-{current_user.username}-{str(uuid.uuid4())[:8]}"
            port_mapping = {"22/tcp": str(assigned_port)}
            mount_dir = os.path.join(os.environ.get("CONTAINER_MOUNT_ROOT", "/amax"), f"gpu-{current_user.username}")
            os.makedirs(mount_dir, exist_ok=True)
            volume_mount = Mount(
                target="/workspace",
                source=mount_dir,
                type="bind",
            )
            try:
                docker_id, docker_status = docker_runner.start_container(
                    image=image_record.image,
                    name=container_name,
                    gpu_ids=gpu_ids,
                    cpu_limit=req.cpu_limit,
                    memory_limit=req.memory_limit,
                    env_vars=req.env_vars,
                    ports=port_mapping,
                    volumes=[volume_mount],
                    ssh_password=access_password,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

            # 7. Create ContainerInstance record
            instance = container_crud.create_container_instance(
                db=db,
                user_id=current_user.id,
                container_id=docker_id,
                image=image_record.image,
                gpu_ids=gpu_ids,
                gpu_count=req.gpu_count,
                cpu_limit=req.cpu_limit,
                memory_limit=req.memory_limit,
                assigned_port=assigned_port,
                access_password=access_password,
                env_vars=req.env_vars,
            )

            # 8. Create GpuAllocation records
            container_crud.create_allocations(db, gpu_ids, instance.id, current_user.id)
            container_crud.record_container_event(db, instance.id, current_user.id, "create", "manual")
    except TimeoutError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return schemas.ContainerResponse(
        id=instance.id,
        container_id=instance.container_id[:12],
        image=instance.image,
        status=instance.status,
        user_id=instance.user_id,
        gpu_ids=instance.gpu_ids,
        gpu_count=instance.gpu_count,
        cpu_limit=instance.cpu_limit,
        memory_limit=instance.memory_limit,
        assigned_port=instance.assigned_port,
        access_password=instance.access_password,
        created_at=instance.created_at,
        started_at=instance.started_at,
    )


@router.delete("/api/containers/{instance_id}", response_model=schemas.Message)
def stop_container(
    instance_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instance = container_crud.get_container_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Container instance not found")

    # Ownership check: admin can stop any, user can stop their own
    if current_user.role != "admin" and instance.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to stop this container",
        )

    if instance.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Container is not running",
        )

    # Stop Docker container (preserve, don't remove)
    success, msg = docker_runner.stop_container(instance.container_id)
    if not success and docker_runner.is_container_running(instance.container_id):
        # Stop failed while the container is still running. Marking the instance
        # stopped / releasing the GPUs here would leave a live container with no
        # DB record, which the UI can then neither stop nor start. Surface it.
        raise HTTPException(
            status_code=500,
            detail=f"Failed to stop Docker container: {msg}",
        )

    # Release GPU allocations
    container_crud.release_allocations_by_container(db, instance.id)

    # Update instance status
    container_crud.stop_container_instance(db, instance.id)

    container_crud.record_container_event(db, instance.id, current_user.id, "stop", "manual")

    return {"message": "Container stopped successfully"}


@router.delete("/api/containers/{instance_id}/remove", response_model=schemas.Message)
def remove_container(
    instance_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instance = container_crud.get_container_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Container instance not found")

    # Ownership check: admin can delete any, user can delete their own
    if current_user.role != "admin" and instance.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this container",
        )

    # Remove Docker container (force stops + removes even if running)
    success, msg = docker_runner.remove_container(instance.container_id)
    if not success:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove Docker container: {msg}",
        )

    # Release GPU allocations (mark as released if not already)
    container_crud.release_allocations_by_container(db, instance.id)

    # Delete DB record
    container_crud.delete_container_instance(db, instance.id)

    container_crud.record_container_event(db, instance.id, current_user.id, "delete", "manual")

    return {"message": "Container deleted successfully"}


@router.post("/api/containers/{instance_id}/start", response_model=schemas.Message)
def start_stopped_container(
    instance_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instance = container_crud.get_container_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Container instance not found")

    # Ownership check
    if current_user.role != "admin" and instance.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to start this container",
        )

    if instance.status != "stopped":
        raise HTTPException(status_code=400, detail="Container is not stopped")

    allocator = GPUAllocator(total_gpu_count=gpu_monitor.get_gpu_count())
    try:
        with allocator.allocate_guard(timeout=int(os.environ.get("ALLOCATION_TIMEOUT", "60"))):
            # If the Docker container is actually still running (e.g. an earlier
            # stop failed after the DB was marked stopped), heal the DB record
            # instead of failing on the busy-GPU check below.
            if docker_runner.is_container_running(instance.container_id):
                if not container_crud.restore_container_instance(db, instance):
                    raise HTTPException(
                        status_code=400,
                        detail="Original GPU(s) are claimed by another active container.",
                    )
                return {"message": "Container is already running"}

            # Check original GPUs are still available (not allocated and not NVML-busy)
            allocated_ids = container_crud.get_allocated_gpu_ids(db)
            raw_statuses = gpu_monitor.get_gpu_status()
            busy_ids = set(
                g["id"] for g in raw_statuses
                if g["id"] not in allocated_ids and (
                    g.get("memory_utilization", 0) > 5 or g.get("gpu_utilization", 0) > 10
                )
            )
            for gid in instance.gpu_ids:
                if gid in allocated_ids or gid in busy_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"GPU {gid} is no longer available. Register a new container instead.",
                    )

            # Re-allocate original GPUs
            container_crud.create_allocations(db, instance.gpu_ids, instance.id, current_user.id)

            # Docker start (preserves container environment)
            success, msg = docker_runner.start_container_by_id(instance.container_id, ssh_password=instance.access_password)
            if not success:
                raise HTTPException(status_code=500, detail=msg)

            # Update status
            container_crud.start_container_instance(db, instance.id)

            container_crud.record_container_event(db, instance.id, current_user.id, "start", "manual")
    except TimeoutError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {"message": "Container started successfully"}


@router.get("/api/containers", response_model=List[schemas.ContainerResponse])
def list_containers(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role == "admin":
        instances = container_crud.get_all_containers(db)
    else:
        instances = container_crud.get_user_containers(db, current_user.id)

    # Sync DB status against the real Docker state (Docker is the source of truth).
    synced = []
    for inst in instances:
        docker_running = docker_runner.is_container_running(inst.container_id)
        if inst.status == "running" and not docker_running:
            # Container was stopped externally (e.g. docker stop/rm, host reboot)
            container_crud.release_allocations_by_container(db, inst.id)
            container_crud.stop_container_instance(db, inst.id)
            inst.status = "stopped"
        elif inst.status == "stopped" and docker_running:
            # Docker is actually running but the DB says stopped (e.g. an earlier
            # stop failed after the DB was updated). Heal so the frontend status
            # and GPU user attribution are consistent with Docker again.
            if container_crud.restore_container_instance(db, inst):
                inst.status = "running"
        synced.append(inst)

    return [
        schemas.ContainerResponse(
            id=inst.id,
            container_id=inst.container_id[:12],
            image=inst.image,
            status=inst.status,
            user_id=inst.user_id,
            gpu_ids=inst.gpu_ids,
            gpu_count=inst.gpu_count,
            cpu_limit=inst.cpu_limit,
            memory_limit=inst.memory_limit,
            assigned_port=inst.assigned_port,
            access_password=inst.access_password,
            created_at=inst.created_at,
            started_at=inst.started_at,
            stopped_at=inst.stopped_at,
        )
        for inst in synced
    ]


@router.get("/api/containers/{instance_id}/logs")
def get_container_logs(
    instance_id: int,
    lines: int = Query(100, ge=1, le=10000),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instance = container_crud.get_container_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Container instance not found")
    if current_user.role != "admin" and instance.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    logs = docker_runner.get_container_logs(instance.container_id, lines=lines)
    return {"logs": logs}


# Admin: image management

@router.post("/api/admin/images", response_model=schemas.GpuImageOut)
def admin_create_image(
    image: schemas.GpuImageCreate,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return container_crud.create_image(db, image, current_user.id)


@router.delete("/api/admin/images/{image_id}", response_model=schemas.Message)
def admin_delete_image(
    image_id: int,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if not container_crud.delete_image(db, image_id):
        raise HTTPException(status_code=404, detail="Image not found")
    return {"message": "Image deleted"}


# ── Admin: Container mount root ──

@router.get("/api/admin/config/mount-root")
def admin_get_mount_root(
    current_user: models.User = Depends(get_current_admin),
):
    mount_root = os.environ.get("CONTAINER_MOUNT_ROOT", "/amax")
    return {"mount_root": mount_root, "read_only": False}
