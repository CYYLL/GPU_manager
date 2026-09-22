from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
import uuid
import secrets
import string
import os
from datetime import datetime
from docker.types import Mount
from docker.errors import APIError, ImageNotFound

from ..database import get_db
from .. import schemas, models
from ..crud import containers as container_crud
from ..crud import users as user_crud
from ..services.gpu_allocator import GPUAllocator
from ..services.docker_runner import DockerRunner
from ..services.gpu_monitor import get_gpu_monitor
from ..services.image_presets import (sync_local_image_presets, local_image_entries,
                                      selected_images, set_selection)
from ..auth import get_current_user, get_current_admin

router = APIRouter(tags=["containers"])

gpu_monitor = get_gpu_monitor()
docker_runner = DockerRunner()


def _docker_gpu_claims(total_gpu_count: int):
    try:
        return docker_runner.get_running_gpu_claims(total_gpu_count)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _gpu_conflict_reason(db: Session, gpu_ids: List[int]) -> Optional[str]:
    """返回 gpu_ids 里第一张『不可复用』卡的中文原因；全部可复用则 None。

    口径与新建容器自动选卡一致：不可复用 = DB 活动分配、运行中的 Docker
    容器 GPU 请求，或 NVML 上有未登记的活动负载。
    restart(stopped→running) 与 rebuild(removed→running) 共用本判定，拒绝消息只在此
    生成，避免两条路径文案漂移。调用方拿到非 None 就以 400 拒绝启动/重建。
    """
    allocated_ids = set(container_crud.get_allocated_gpu_ids(db))
    docker_claims = _docker_gpu_claims(gpu_monitor.get_gpu_count())
    holder_of = {}
    if allocated_ids:
        rows = db.query(models.GpuAllocation).filter(
            models.GpuAllocation.gpu_id.in_(sorted(allocated_ids)),
            models.GpuAllocation.released_at.is_(None),
        ).all()
        holder_of = {a.gpu_id: a.container_instance_id for a in rows}
    raw_statuses = gpu_monitor.get_gpu_status() or []
    busy_ids = {
        g["id"] for g in raw_statuses
        if g["id"] not in allocated_ids and (
            g.get("memory_utilization", 0) > 5 or g.get("gpu_utilization", 0) > 10
        )
    }
    for gid in gpu_ids:
        holder_id = holder_of.get(gid)
        if holder_id is not None:
            holder = db.query(models.ContainerInstance).filter(
                models.ContainerInstance.id == holder_id).first()
            if holder is not None:
                owner = holder.user.username if holder.user else "?"
                return (f"GPU {gid} 正被另一个容器占用（container {holder.container_id[:12]}，"
                        f"用户 {owner}，状态 {holder.status}）。请先停止该容器再启动/重建。")
            return (f"GPU {gid} 已被另一个容器占用，无法启动/重建。"
                    f"请先停止占用该卡的容器再重试。")
        if docker_claims.get(gid):
            holder = docker_claims[gid][0]
            return (f"GPU {gid} 已被运行中的 Docker 容器 "
                    f"{holder['name']} ({holder['container_id'][:12]}) 预留，"
                    "请先处理该容器的 GPU 占用。")
        if gid in busy_ids:
            return (f"GPU {gid} 正被未登记占用者的进程使用（显存/算力有活动负载），"
                    f"无法启动/重建。请稍后再试，或改用其它空闲卡。")
    return None


@router.get("/api/images", response_model=List[schemas.GpuImageOut])
def list_images(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sync_local_image_presets(db, docker_runner.client)
    return selected_images(db, current_user.id)


@router.get("/api/images/available")
def list_available_images(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        entries = local_image_entries(docker_runner.client)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    sync_local_image_presets(db, docker_runner.client, entries)
    catalog = {row.image: row for row in container_crud.get_images(db)}
    selected = {row[0] for row in db.query(models.UserImagePreset.image_ref).filter_by(
        user_id=current_user.id).all()}
    return sorted(({"id": catalog[entry["image_ref"]].id,
                    "image_ref": entry["image_ref"], "size_bytes": entry["size_bytes"],
                    "selected": entry["image_ref"] in selected}
                   for entry in entries if entry["image_ref"] in catalog),
                  key=lambda row: row["id"])


@router.put("/api/images/presets")
def update_image_preset(
    req: schemas.ImagePresetSelection,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        set_selection(db, current_user.id, req.image_ref, req.selected,
                      docker_runner.client)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"image_ref": req.image_ref, "selected": req.selected}


@router.post("/api/containers/start", response_model=schemas.ContainerResponse)
def start_container(
    req: schemas.ContainerStartRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _start_container_impl(req, current_user, db, "manual")


def _start_container_impl(req: schemas.ContainerStartRequest, current_user: models.User,
                          db: Session, source: str = "manual"):
    # 1. Validate image exists
    image_record = container_crud.get_image_by_id(db, req.image_id)
    if not image_record:
        raise HTTPException(status_code=404, detail="Image not found")
    if not db.query(models.UserImagePreset).filter_by(
            user_id=current_user.id, image_ref=image_record.image).first():
        raise HTTPException(status_code=403, detail="请先将该本地镜像加入自己的预设")

    # 2. Validate GPU count >= image minimum
    if req.gpu_count < image_record.min_gpu:
        raise HTTPException(
            status_code=400,
            detail=f"镜像预设最低需要 {image_record.min_gpu} 张 GPU，当前请求 {req.gpu_count} 张",
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
            docker_claims = _docker_gpu_claims(allocator.total_gpu_count)
            available = allocator.find_available_gpus(
                db, req.gpu_count, busy_gpu_ids=list(set(busy_ids) | set(docker_claims)))
            if len(available) < req.gpu_count:
                raise HTTPException(
                    status_code=400,
                    detail=(f"GPU 不足：请求 {req.gpu_count} 张，可用 {len(available)} 张。"
                            f"Docker 容器预留的 GPU：{sorted(docker_claims)}"),
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
                docker_id, docker_status, ssh_username = docker_runner.start_container(
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
                ssh_username=ssh_username,
                env_vars=req.env_vars,
            )

            # 8. Create GpuAllocation records
            container_crud.create_allocations(db, gpu_ids, instance.id, current_user.id)
            container_crud.record_container_event(db, instance.id, current_user.id, "create", source)
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
        ssh_username=instance.ssh_username,
        created_at=instance.created_at,
        started_at=instance.started_at,
    )


@router.delete("/api/containers/{instance_id}", response_model=schemas.Message)
def stop_container(instance_id: int, current_user: models.User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    return _stop_container_impl(instance_id, current_user, db, "manual")


def _stop_container_impl(instance_id: int, current_user: models.User, db: Session,
                         source: str = "manual"):
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
    if not success and docker_runner.is_container_running(instance.container_id) is not False:
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

    container_crud.record_container_event(db, instance.id, current_user.id, "stop", source)

    return {"message": "Container stopped successfully"}


@router.delete("/api/containers/{instance_id}/remove", response_model=schemas.Message)
def remove_container(
    instance_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _remove_container_impl(instance_id, current_user, db, "manual")


def _remove_container_impl(instance_id: int, current_user: models.User, db: Session,
                           source: str = "manual"):
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

    container_crud.record_container_event(db, instance.id, current_user.id, "delete", source)

    return {"message": "Container deleted successfully"}


@router.post("/api/containers/{instance_id}/start", response_model=schemas.Message)
def start_stopped_container(
    instance_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _start_stopped_container_impl(instance_id, current_user, db, "manual")


def _start_stopped_container_impl(instance_id: int, current_user: models.User, db: Session,
                                  source: str = "manual"):
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
            docker_state = docker_runner.is_container_running(instance.container_id)
            if docker_state is None:
                raise HTTPException(status_code=503, detail="无法确认 Docker 容器状态，请稍后重试")
            if docker_state:
                if instance.access_password:
                    ssh_ok, ssh_username, ssh_reason = docker_runner.ensure_ssh(
                        instance.container_id, instance.access_password, instance.ssh_username)
                    if not ssh_ok:
                        raise HTTPException(status_code=503, detail=f"容器 SSH 恢复失败：{ssh_reason}")
                    instance.ssh_username = ssh_username
                if not container_crud.restore_container_instance(db, instance):
                    raise HTTPException(
                        status_code=400,
                        detail=("该容器在 Docker 里仍在运行，但其记录的 GPU 已被另一台容器占用，"
                                "无法恢复运行标记。请先停止占用该卡的容器，或删除本记录。"),
                    )
                return {"message": "Container is already running"}

            # Check original GPUs are still available (not allocated and not NVML-busy)
            reason = _gpu_conflict_reason(db, instance.gpu_ids)
            if reason:
                raise HTTPException(status_code=400, detail=reason)

            # Docker start (preserves container environment)
            success, msg = docker_runner.start_container_by_id(
                instance.container_id, ssh_password=instance.access_password,
                ssh_username=instance.ssh_username)
            if not success:
                raise HTTPException(status_code=500, detail=msg)
            if msg != "running":
                instance.ssh_username = msg
            container_crud.create_allocations(db, instance.gpu_ids, instance.id, current_user.id)

            # Update status
            container_crud.start_container_instance(db, instance.id)

            container_crud.record_container_event(db, instance.id, current_user.id, "start", source)
    except TimeoutError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {"message": "Container started successfully"}


@router.post("/api/containers/{instance_id}/rebuild", response_model=schemas.ContainerResponse)
def rebuild_container(instance_id: int,
                      current_user: models.User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    return _rebuild_container_impl(instance_id, current_user, db, "manual")


def _rebuild_container_impl(instance_id: int, current_user: models.User, db: Session,
                            source: str = "manual"):
    """Recreate a `removed` container from its stored config snapshot."""
    instance = container_crud.get_container_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Container instance not found")
    if current_user.role != "admin" and instance.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to rebuild this container")
    if instance.status != "removed":
        raise HTTPException(status_code=400, detail="Container is not in removed state")

    allocator = GPUAllocator(total_gpu_count=gpu_monitor.get_gpu_count())
    try:
        with allocator.allocate_guard(timeout=int(os.environ.get("ALLOCATION_TIMEOUT", "60"))):
            reason = _gpu_conflict_reason(db, instance.gpu_ids)
            if reason:
                raise HTTPException(status_code=400, detail=reason)

            # port: reuse if still free (not held by another container), else reallocate
            used_ports = set()
            for (p,) in db.query(models.ContainerInstance.assigned_port).filter(
                models.ContainerInstance.assigned_port.isnot(None),
                models.ContainerInstance.id != instance.id,
            ).all():
                used_ports.add(p)
            assigned_port = instance.assigned_port
            if assigned_port is None or assigned_port in used_ports:
                # exclude_id=本行：低水位自动回收不得误删正在重建的 removed 快照
                assigned_port = container_crud.allocate_port(db, exclude_id=instance.id)
                instance.assigned_port = assigned_port

            container_name = f"gpu-{current_user.username}-{str(uuid.uuid4())[:8]}"
            port_mapping = {"22/tcp": str(assigned_port)}
            mount_dir = os.path.join(os.environ.get("CONTAINER_MOUNT_ROOT", "/amax"),
                                     f"gpu-{current_user.username}")
            os.makedirs(mount_dir, exist_ok=True)
            volume_mount = Mount(target="/workspace", source=mount_dir, type="bind")

            try:
                docker_id, _docker_status, ssh_username = docker_runner.start_container(
                    image=instance.image,
                    name=container_name,
                    gpu_ids=instance.gpu_ids,
                    cpu_limit=instance.cpu_limit,
                    memory_limit=instance.memory_limit,
                    env_vars=instance.env_vars or {},
                    ports=port_mapping,
                    volumes=[volume_mount],
                    ssh_password=instance.access_password,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

            instance.container_id = docker_id
            instance.ssh_username = ssh_username
            instance.status = "running"
            instance.started_at = datetime.utcnow()
            instance.stopped_at = None
            db.commit()
            db.refresh(instance)
            container_crud.create_allocations(db, instance.gpu_ids, instance.id, current_user.id)
            container_crud.record_container_event(db, instance.id, current_user.id,
                                                  "rebuild", source)
    except TimeoutError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return schemas.ContainerResponse(
        id=instance.id, container_id=instance.container_id[:12], image=instance.image,
        status=instance.status, user_id=instance.user_id, gpu_ids=instance.gpu_ids,
        gpu_count=instance.gpu_count, cpu_limit=instance.cpu_limit,
        memory_limit=instance.memory_limit, assigned_port=instance.assigned_port,
        access_password=instance.access_password, created_at=instance.created_at,
        ssh_username=instance.ssh_username,
        started_at=instance.started_at,
    )


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
        if inst.status == "running" and docker_running is False:
            # Container was stopped externally (e.g. docker stop/rm, host reboot)
            container_crud.release_allocations_by_container(db, inst.id)
            container_crud.stop_container_instance(db, inst.id)
            inst.status = "stopped"
        elif inst.status == "stopped" and docker_running is True:
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
            ssh_username=inst.ssh_username,
            cleanup_protected=inst.cleanup_protected,
            created_at=inst.created_at,
            started_at=inst.started_at,
            stopped_at=inst.stopped_at,
        )
        for inst in synced
    ]


@router.post("/api/containers/{instance_id}/ssh/repair")
def repair_container_ssh(instance_id: int,
                         current_user: models.User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    return _repair_container_ssh_impl(instance_id, current_user, db, "manual")


def _repair_container_ssh_impl(instance_id: int, current_user: models.User,
                               db: Session, source: str = "manual"):
    instance = container_crud.get_container_instance(db, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="容器不存在")
    if current_user.role != "admin" and instance.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权修复此容器的 SSH 登录")
    if instance.status != "running" or docker_runner.is_container_running(instance.container_id) is not True:
        raise HTTPException(status_code=400, detail="容器未运行")
    if not instance.access_password:
        raise HTTPException(status_code=400, detail="容器没有已保存的访问密码")
    ok, username, reason = docker_runner.setup_ssh(
        instance.container_id, instance.access_password, instance.ssh_username)
    if not ok:
        raise HTTPException(status_code=500, detail=reason)
    instance.ssh_username = username
    db.commit()
    container_crud.record_container_event(db, instance.id, current_user.id, "ssh_repair", source)
    return {"ssh_username": username, "assigned_port": instance.assigned_port}


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


class ProtectionUpdate(BaseModel):
    protected: bool


@router.put("/api/containers/{instance_id}/protection")
def set_protection(instance_id: int, req: ProtectionUpdate,
                   current_user: models.User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    inst = container_crud.get_container_instance(db, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Container instance not found")
    if current_user.role != "admin" and inst.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    inst.cleanup_protected = req.protected
    db.commit()
    return {"protected": inst.cleanup_protected}


# Admin: image management

@router.post("/api/admin/images", response_model=schemas.GpuImageOut)
def admin_create_image(
    image: schemas.GpuImageCreate,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    try:
        set_selection(db, current_user.id, image.image, True, docker_runner.client)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    row = db.query(models.GpuImage).filter_by(image=image.image).first()
    row.name = image.name
    row.description = image.description or ""
    row.min_gpu = image.min_gpu or 1
    row.recommended_gpu = image.recommended_gpu or 1
    db.commit()
    return row


@router.delete("/api/admin/images/{image_id}", response_model=schemas.Message)
def admin_delete_image(
    image_id: int,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    image = container_crud.get_image_by_id(db, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found")
    db.query(models.UserImagePreset).filter_by(
        user_id=current_user.id, image_ref=image.image).delete()
    db.commit()
    return {"message": "预设已移除，本地镜像保留"}


def _delete_local_image_impl(image_id: int, db: Session):
    """Remove one local image tag after checking every Docker container."""
    image = container_crud.get_image_by_id(db, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="预设镜像不存在")
    image_ref = image.image
    if docker_runner.client is None:
        raise HTTPException(status_code=503, detail="Docker 服务不可用，无法删除本地镜像")
    try:
        local = docker_runner.client.images.get(image_ref)
    except ImageNotFound:
        raise HTTPException(status_code=404, detail="本地镜像不存在；可将它从个人预设中移出")
    except APIError as exc:
        raise HTTPException(status_code=503, detail=f"查询本地镜像失败：{exc}")

    try:
        # Docker's ancestor filter covers containers using this image ID, even
        # when they were created from another tag of the same image.
        used_by = docker_runner.client.containers.list(
            all=True, filters={"ancestor": local.id})
    except APIError as exc:
        raise HTTPException(status_code=503, detail=f"检查镜像占用失败，已拒绝删除：{exc}")
    if used_by:
        raise HTTPException(
            status_code=409,
            detail=f"警告：镜像 {image_ref} 正被 {len(used_by)} 个容器使用（包括已停止容器），已拒绝删除。请先处理这些容器。",
        )

    try:
        docker_runner.client.images.remove(image_ref, force=False)
    except ImageNotFound:
        raise HTTPException(status_code=404, detail="本地镜像已不存在")
    except APIError as exc:
        raise HTTPException(status_code=409, detail=f"镜像删除失败，可能仍被容器或构建任务引用：{exc}")
    # Multiple preset rows can refer to the same tag. Remove all stale rows.
    db.query(models.GpuImage).filter(models.GpuImage.image == image_ref).delete(
        synchronize_session=False)
    db.expunge(image)
    db.query(models.UserImagePreset).filter_by(image_ref=image_ref).delete()
    container_crud.renumber_image_ids(db)
    db.commit()
    return {"message": f"已删除本地镜像标签 {image_ref} 及对应预设记录；共享镜像层可能仍被其他标签保留"}


@router.delete("/api/admin/images/{image_id}/local", response_model=schemas.Message)
def admin_delete_local_image(
    image_id: int,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return _delete_local_image_impl(image_id, db)


# ── Admin: Container mount root ──

@router.get("/api/admin/config/mount-root")
def admin_get_mount_root(
    current_user: models.User = Depends(get_current_admin),
):
    mount_root = os.environ.get("CONTAINER_MOUNT_ROOT", "/amax")
    return {"mount_root": mount_root, "read_only": False}
