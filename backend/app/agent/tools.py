"""Agent tools: read-only queries + protection toggle + container lifecycle.

Lifecycle tools delegate to the containers router _impl handlers
(source="llm") so ownership / quota / availability checks live in exactly one
place. The LLM may call these via the chat route; it can never bypass checks.
"""
import os
import shutil
from typing import List, Dict, Tuple

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..crud import containers as container_crud
from ..services.docker_runner import DockerRunner
from ..services.gpu_monitor import get_gpu_monitor
from ..routers import containers as containers_router

docker_runner = DockerRunner()
gpu_monitor = get_gpu_monitor()

TOOLS: List[Dict] = [
    {"name": "list_containers", "description": "列出当前用户的所有容器",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_container_status", "description": "查询指定容器的实时状态",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "get_gpu_status", "description": "查询当前 GPU 状态",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_disk_status", "description": "查询宿主磁盘水位",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_images", "description": "列出可用的预设镜像（创建容器时用其中的 id）",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_container_protection", "description": "设置或取消某容器的清理保护",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}, "protected": {"type": "boolean"}},
         "required": ["id", "protected"]}},
    {"name": "create_container", "description": "创建并启动一个新容器",
     "input_schema": {"type": "object", "properties": {
         "image_id": {"type": "integer"},
         "gpu_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
         "cpu_limit": {"type": "number"}, "memory_limit": {"type": "integer"},
         "env_vars": {"type": "object"}}, "required": ["image_id"]}},
    {"name": "start_container", "description": "启动一个已停止的容器",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "stop_container", "description": "停止一个运行中的容器",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "delete_container", "description": "删除一个容器（不可恢复，会 force 移除；保留工作区数据但无配置快照；操作前确认）",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
]

_SRC = "llm"


class ToolExecutor:
    def __init__(self, db: Session, user: models.User):
        self.db = db
        self.user = user

    def run(self, name: str, inp: Dict) -> Tuple[bool, str]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return False, f"Unknown tool: {name}"
        try:
            result = handler(inp)
            self.db.commit()  # release any implicit read txn before the next external call
            return result
        except Exception as e:
            self.db.rollback()  # surface to LLM, never crash, never leak an open txn
            return False, f"tool error: {e}"

    def _owner_or_admin(self, inst) -> bool:
        return self.user.role == "admin" or inst.user_id == self.user.id

    # ── tools ──────────────────────────────────────────────
    def _tool_list_containers(self, inp) -> Tuple[bool, str]:
        insts = container_crud.get_user_containers(self.db, self.user.id)
        lines = []
        for i in insts:
            running = docker_runner.is_container_running(i.container_id)
            lines.append(
                f"- id={i.id} container_id={i.container_id} image={i.image} "
                f"status={i.status} docker_running={running} gpu={i.gpu_ids} "
                f"protected={i.cleanup_protected} port={i.assigned_port}"
            )
        return True, "\n".join(lines) if lines else "（没有容器）"

    def _tool_get_container_status(self, inp) -> Tuple[bool, str]:
        inst = container_crud.get_container_instance(self.db, int(inp["id"]))
        if inst is None:
            return False, "容器不存在"
        if not self._owner_or_admin(inst):
            return False, "未授权：只能查看自己的容器"
        running = docker_runner.is_container_running(inst.container_id)
        return True, (f"id={inst.id} image={inst.image} status={inst.status} "
                      f"docker_running={running} gpu={inst.gpu_ids} "
                      f"last_used_ts={container_crud.get_last_used(self.db, inst.id)}")

    def _tool_get_gpu_status(self, inp) -> Tuple[bool, str]:
        allocated = container_crud.get_allocated_gpu_ids(self.db)
        statuses = gpu_monitor.get_gpu_status(allocated_gpu_ids=allocated)
        if not statuses:
            return True, "（无法读取 GPU 状态）"
        lines = [f"- gpu={g['id']} name={g['name']} mem={g['memory_utilization']}% "
                 f"util={g['gpu_utilization']}% busy_allocated={g['allocated']}"
                 for g in statuses]
        return True, "\n".join(lines)

    def _tool_get_disk_status(self, inp) -> Tuple[bool, str]:
        usage = shutil.disk_usage(os.environ.get("CONTAINER_MOUNT_ROOT", "/amax"))
        pct = usage.used / usage.total * 100
        return True, (f"total={usage.total} used={usage.used} free={usage.free} "
                      f"usage_pct={pct:.1f}%")

    def _tool_list_images(self, inp) -> Tuple[bool, str]:
        images = container_crud.get_images(self.db)
        lines = [f"- id={im.id} name={im.name} image={im.image} min_gpu={im.min_gpu}"
                 for im in images]
        return True, "\n".join(lines) if lines else "（没有可用镜像）"

    def _tool_set_container_protection(self, inp) -> Tuple[bool, str]:
        inst = container_crud.get_container_instance(self.db, int(inp["id"]))
        if inst is None:
            return False, "容器不存在"
        if not self._owner_or_admin(inst):
            return False, "未授权：只能操作自己的容器"
        inst.cleanup_protected = bool(inp["protected"])
        self.db.commit()
        return True, f"已{'设置' if inp['protected'] else '取消'}保护"

    # ── mutating tools (delegate to lifecycle _impl handlers; HTTPException → (False, detail))
    def _tool_start_container(self, inp) -> Tuple[bool, str]:
        try:
            result = containers_router._start_stopped_container_impl(int(inp["id"]), self.user, self.db, "llm")
            return True, result.get("message", "started")
        except HTTPException as e:
            return False, e.detail

    def _tool_stop_container(self, inp) -> Tuple[bool, str]:
        try:
            result = containers_router._stop_container_impl(int(inp["id"]), self.user, self.db, "llm")
            return True, result.get("message", "stopped")
        except HTTPException as e:
            return False, e.detail

    def _tool_delete_container(self, inp) -> Tuple[bool, str]:
        try:
            result = containers_router._remove_container_impl(int(inp["id"]), self.user, self.db, "llm")
            return True, result.get("message", "deleted")
        except HTTPException as e:
            return False, e.detail

    def _tool_create_container(self, inp) -> Tuple[bool, str]:
        try:
            req = schemas.ContainerStartRequest(
                image_id=int(inp["image_id"]),
                gpu_count=int(inp.get("gpu_count", 1)),
                cpu_limit=inp.get("cpu_limit"),
                memory_limit=inp.get("memory_limit"),
                env_vars=inp.get("env_vars") or {},
            )
            resp = containers_router._start_container_impl(req, self.user, self.db, "llm")
            return True, (f"容器已创建 id={resp.id} image={resp.image} status={resp.status} "
                          f"port={resp.assigned_port} password={resp.access_password}")
        except HTTPException as e:
            return False, e.detail
        except ValidationError as e:
            return False, str(e)
