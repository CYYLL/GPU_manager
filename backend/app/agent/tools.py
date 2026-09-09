"""Agent tools: read-only queries + protection toggle + container lifecycle.

Lifecycle tools delegate to the containers router _impl handlers
(source="llm") so ownership / quota / availability checks live in exactly one
place. The LLM may call these via the chat route; it can never bypass checks.
"""
import json
import os
import shutil
from typing import List, Dict, Tuple

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..crud import containers as container_crud
from ..crud import users as user_crud
from ..services.docker_runner import DockerRunner
from ..services.gpu_monitor import get_gpu_monitor
from ..routers import containers as containers_router

docker_runner = DockerRunner()
gpu_monitor = get_gpu_monitor()

# 空 stub schema：只让模型能"按名发起调用"；真实参数靠拦截后回注完整 schema、
# 下一轮全量声明来引导 —— 未激活工具的占位调用不会被执行（见 agent_loop 的拦截逻辑）。
_EMPTY_SCHEMA = {"type": "object", "properties": {}}

# 单一注册点：每加一个工具只在 _TOOL_DEFS 加一个 dict。
#   summary        —— 给 stub 的一句话摘要（每轮发给模型）
#   description    —— 完整说明（工具激活后才发给模型）
#   input_schema   —— 完整参数 schema（同上）
# TOOLS / TOOL_SUMMARIES / ToolCatalog 均由它派生，加工具不动别处。
_TOOL_DEFS: List[Dict] = [
    {"name": "list_containers",
     "summary": "列出容器（普通用户=自己的；管理员=全部，带 user=）",
     "description": "列出容器（普通用户只看到自己的容器，管理员可看到所有用户的所有容器）",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_container_status",
     "summary": "按 id 查询单个容器的实时状态",
     "description": "查询指定容器的实时状态",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "get_gpu_status",
     "summary": "查询当前 GPU 使用与空闲状态（被占用的卡带 user= 占用者）",
     "description": "查询当前 GPU 状态：每张卡输出内存/利用率；空闲卡标 free；"
     "被占用的卡标 user= 占用者用户名（与 GPU 看板一致，见可见性说明）",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_disk_status",
     "summary": "查询宿主磁盘用量与剩余",
     "description": "查询宿主磁盘水位",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_images",
     "summary": "列出可创建容器的预设镜像（创建时用其 id）",
     "description": "列出可用的预设镜像（创建容器时用其中的 id）",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_container_protection",
     "summary": "设置/取消某容器的清理保护（id, protected）",
     "description": "设置或取消某容器的清理保护",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}, "protected": {"type": "boolean"}},
         "required": ["id", "protected"]}},
    {"name": "check_gpu_quota",
     "summary": "检查 GPU 配额：本账号；管理员可带 username 查指定用户",
     "description": "检查 GPU 配额：总配额 / 已用 / 剩余可创建数。"
     "创建容器前若不确定能申请几张卡，先调用本工具并传入要申请的 gpu_count，确认不超过剩余配额，"
     "避免创建失败。管理员无配额限制；管理员若带 username，则查询该指定用户的配额"
     "（该参数对普通用户无效，会被拒绝）。",
     "input_schema": {"type": "object", "properties": {
         "gpu_count": {"type": "integer", "minimum": 1},
         "username": {"type": "string"}}}},
    {"name": "set_user_quota",
     "summary": "修改某用户的 GPU 配额上限（仅管理员，0-4 张）",
     "description": "将指定用户（username）的 GPU 配额上限设为 gpu_quota（0-4 张卡）。"
     "仅管理员可调用，普通用户调用会被拒绝；不适用于管理员账号。"
     "只影响该用户后续能否新建容器，不会停止其已运行的容器。",
     "input_schema": {"type": "object", "properties": {
         "username": {"type": "string"},
         "gpu_quota": {"type": "integer", "minimum": 0, "maximum": 4}},
         "required": ["username", "gpu_quota"]}},
    {"name": "list_users",
     "summary": "列出所有普通用户及其配额/用量/容器概况（仅管理员）",
     "description": "列出系统里所有普通用户（不含管理员）：username、mode(双模式)、"
     "gpu_quota、gpu_used(当前占卡数)、containers(容器记录数)/running(运行中数)。"
     "没有容器、没有 GPU 分配的用户也会出现——他们不会在容器/GPU 查询结果里冒头。"
     "仅管理员可调用，普通用户调用会被拒绝。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "delete_user",
     "summary": "删除一个普通用户及其容器与数据库记录（仅管理员，不可恢复）",
     "description": "删除一个普通用户（username，与 User management 页删除同口径、只针对普通用户）："
     "先停止并移除该用户名下所有 docker 容器本体，再清除其全部数据库记录"
     "（容器记录、GPU 分配、其创建的镜像、账号）。删除后用户 id 会重排。"
     "绝不删除宿主机上的工作区挂载目录（CONTAINER_MOUNT_ROOT 下的 gpu-<username> 保留，"
     "该用户名被重新注册时可复用同一工作区）。"
     "仅管理员可调用，普通用户调用会被拒绝；管理员账号不能作为删除目标；"
     "删除不可恢复，动手前必须先向用户本人明确确认。",
     "input_schema": {"type": "object", "properties": {
         "username": {"type": "string"}}, "required": ["username"]}},
    {"name": "create_container",
     "summary": "按镜像创建并启动容器（image_id 必填；可配 gpu_count 等）",
     "description": "创建并启动一个新容器",
     "input_schema": {"type": "object", "properties": {
         "image_id": {"type": "integer"},
         "gpu_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
         "cpu_limit": {"type": "number"}, "memory_limit": {"type": "integer"},
         "env_vars": {"type": "object"}}, "required": ["image_id"]}},
    {"name": "start_container",
     "summary": "启动一个已停止的容器（id）",
     "description": "启动一个已停止的容器",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "stop_container",
     "summary": "停止一个运行中的容器（id）",
     "description": "停止一个运行中的容器",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "delete_container",
     "summary": "删除容器（不可恢复，删除前须向用户确认）",
     "description": "删除一个容器（不可恢复，会 force 移除；保留工作区数据但无配置快照；操作前确认）",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "rebuild_container",
     "summary": "重建一个已删除(removed)的容器（按 id，用原配置快照）",
     "description": "重建一个已删除(removed)的容器（用原配置快照）",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}},
]

# 给 API / agent 循环的完整定义：只含网关接受的三键（name/description/input_schema）。
# Anthropic 链路原样透传，多出的键（如 summary）可能被网关拒收 → 绝不在 TOOLS 里带多余键。
TOOLS: List[Dict] = [
    {k: d[k] for k in ("name", "description", "input_schema")} for d in _TOOL_DEFS
]

# 每个工具的一句话摘要：stub 的 description。加工具仍只动 _TOOL_DEFS 一处。
TOOL_SUMMARIES: Dict[str, str] = {d["name"]: d["summary"] for d in _TOOL_DEFS}

_SRC = "llm"

# 管理员账户在 agent 里只能停止/删除用户的容器；create/start/rebuild（会产生或
# 恢复一个运行中容器的动作）一律拒绝 —— 管理员不持有自己的 GPU 容器。
_ADMIN_CONTAINER_FORBIDDEN = "管理员账户不能创建/启动/重建容器，只能停止和删除用户的容器"


def _pct(v) -> str:
    """Round a utilization percentage to an integer for terse output."""
    try:
        return f"{float(v):.0f}"
    except (TypeError, ValueError):
        return "?"


def _short_gpu_name(name) -> str:
    """Trim verbose vendor prefixes so GPU lines stay short (RTX 3090, not
    NVIDIA GeForce RTX 3090). Falls back to the original label."""
    n = (name or "").strip()
    low = n.lower()
    for prefix in ("nvidia geforce ", "nvidia tesla ", "nvidia quadro ", "nvidia "):
        if low.startswith(prefix):
            n = n[len(prefix):]
            break
    return n or (name or "")


# "空闲卡"判定阈值，与 /api/gpus/status 的默认口径一致（内存 <=5% 且利用率 <=10%
# 视为未被使用；该端点可用环境变量覆盖，此处保持默认以对齐 GPU 看板语义）。
_FREE_GPU_MEM_PCT = 5.0
_FREE_GPU_UTIL_PCT = 10.0


def _human_bytes(b: float) -> str:
    """Compact human size: 12.3G / 456M / 890K, avoiding 20-char ints."""
    try:
        b = float(b)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(b) < 1024:
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}E"


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

    def _admin_mutation_block(self):
        """管理员在 agent 里只允许 stop/delete：会"产生/恢复运行容器"的变更返回
        拒绝元组 (False, msg)；非管理员返回 None（放行到真正的生命周期逻辑）。"""
        if self.user.role == "admin":
            return (False, _ADMIN_CONTAINER_FORBIDDEN)
        return None

    # ── tools ──────────────────────────────────────────────
    def _tool_list_containers(self, inp) -> Tuple[bool, str]:
        # 普通用户只能看到自己的容器；admin 可枚举所有用户的所有容器(每行标注 user= 所属)。
        is_admin = self.user.role == "admin"
        insts = (container_crud.get_all_containers(self.db)
                 if is_admin
                 else container_crud.get_user_containers(self.db, self.user.id))
        lines = []
        for i in insts:
            running = docker_runner.is_container_running(i.container_id)
            owner = f" user={i.user.username}" if is_admin else ""
            # removed 快照在清理时已释放端口(assigned_port=None)，不再打印 port=None
            port_txt = "" if i.assigned_port is None else f" port={i.assigned_port}"
            lines.append(
                f"- id={i.id} cid={i.container_id[:12]} image={i.image} "
                f"status={i.status} docker_running={running} gpu={i.gpu_ids} "
                f"protected={i.cleanup_protected}{port_txt}{owner}"
            )
        return True, "\n".join(lines) if lines else "（没有容器）"

    def _tool_get_container_status(self, inp) -> Tuple[bool, str]:
        inst = container_crud.get_container_instance(self.db, int(inp["id"]))
        if inst is None:
            return False, "容器不存在"
        if not self._owner_or_admin(inst):
            return False, "未授权：只能查看自己的容器"
        running = docker_runner.is_container_running(inst.container_id)
        owner = f" user={inst.user.username}" if self.user.role == "admin" else ""
        return True, (f"id={inst.id} image={inst.image} status={inst.status} "
                      f"docker_running={running} gpu={inst.gpu_ids} "
                      f"protected={inst.cleanup_protected} "
                      f"last_used_ts={container_crud.get_last_used(self.db, inst.id)}{owner}")

    def _tool_get_gpu_status(self, inp) -> Tuple[bool, str]:
        # 与 /api/gpus/status（Dashboard 同源）一致：把 GpuAllocation 的实时占用归属
        # （gpu_id → 用户名）接到 NVML 状态上，让 agent 能回答"谁在用哪些卡/剩几张空闲"。
        # 占用者用户名对所有登录用户可见（GPU 看板同样如此），这里不区分角色；
        # 也不输出容器 id/镜像等更细信息（仅用户名）。
        allocated = container_crud.get_allocated_gpu_ids(self.db)
        owner: Dict[int, str] = {}
        if allocated:
            rows = (self.db.query(models.GpuAllocation)
                    .filter(models.GpuAllocation.gpu_id.in_(allocated),
                            models.GpuAllocation.released_at.is_(None))
                    .all())
            owner = {r.gpu_id: r.user.username for r in rows}
        statuses = gpu_monitor.get_gpu_status(
            allocated_gpu_ids=allocated, allocation_users=owner)
        if not statuses:
            return True, "（无法读取 GPU 状态）"
        lines = []
        for g in statuses:
            base = (f"- gpu={g['id']} {_short_gpu_name(g['name'])} "
                    f"mem={_pct(g['memory_utilization'])}% "
                    f"util={_pct(g['gpu_utilization'])}%")
            if g.get("error"):
                lines.append(base + " error")
            elif g["allocated"]:
                # 有未释放的分配 → 该卡被登记占用，标出占用者用户名。
                who = owner.get(g["id"]) or g.get("allocated_to") or "?"
                lines.append(f"{base} user={who}")
            elif (g["memory_utilization"] <= _FREE_GPU_MEM_PCT
                  and g["gpu_utilization"] <= _FREE_GPU_UTIL_PCT):
                lines.append(base + " free")
            else:
                # NVML 有负载但库里无分配（外部/未登记进程）→ 无法归属到用户名。
                lines.append(base + " busy(未登记)")
        return True, "\n".join(lines)

    def _tool_get_disk_status(self, inp) -> Tuple[bool, str]:
        usage = shutil.disk_usage(os.environ.get("CONTAINER_MOUNT_ROOT", "/amax"))
        pct = usage.used / usage.total * 100
        return True, (f"usage_pct={pct:.1f}% used={_human_bytes(usage.used)} "
                      f"free={_human_bytes(usage.free)} total={_human_bytes(usage.total)}")

    def _tool_list_images(self, inp) -> Tuple[bool, str]:
        images = container_crud.get_images(self.db)
        lines = [f"- id={im.id} name={im.name} image={im.image} min_gpu={im.min_gpu}"
                 for im in images]
        return True, "\n".join(lines) if lines else "（没有可用镜像）"

    def _tool_check_gpu_quota(self, inp) -> Tuple[bool, str]:
        """配额检查：总配额 / 已用 / 剩余可申请，管理员无配额限制。

        与创建容器的强制校验用同一把尺子（allocator.check_quota 同为
        gpu_quota - get_user_gpu_used），故这里报“可以”即创建不会因配额被拒。
        管理员带 username 时查询该指定用户的配额（普通用户带 username 被拒绝）。
        """
        requested = int(inp["gpu_count"]) if inp.get("gpu_count") is not None else None
        target_name = (inp.get("username") or "").strip()
        if target_name:
            if self.user.role != "admin":
                return False, "仅管理员可查询其他用户的配额"
            target = user_crud.get_user_by_username_ci(self.db, target_name)
            if target is None:
                return False, f"用户不存在：{target_name}"
            quota = target.gpu_quota or 0
            used = container_crud.get_user_gpu_used(self.db, target.id)
            remaining = max(quota - used, 0)
            base = f"用户 {target.username}：配额={quota} 已用={used} 剩余={remaining}"
            if requested is None:
                return True, base
            fits = requested <= remaining
            return True, (base + f"；申请 {requested} 张卡="
                          f"{'可以' if fits else '不可以，超过剩余配额'}（剩余 {remaining}）")
        if self.user.role == "admin":
            msg = "管理员无 GPU 配额限制"
            if requested is None:
                return True, msg
            return True, f"{msg}；申请 {requested} 张卡是否成功取决于物理 GPU 空闲数量"
        quota = self.user.gpu_quota or 0
        used = container_crud.get_user_gpu_used(self.db, self.user.id)
        remaining = max(quota - used, 0)
        if requested is None:
            return True, f"配额={quota} 已用={used} 剩余={remaining}"
        fits = requested <= remaining
        hint = (f"申请 {requested} 张卡={'可以' if fits else '不可以，超过剩余配额'}"
                f"（剩余 {remaining}）")
        return True, hint

    def _tool_set_user_quota(self, inp) -> Tuple[bool, str]:
        """管理员专用：修改指定用户的 GPU 配额上限（0-4）。

        与 /api/admin/users/{user_id}/quota 同一把尺子（0<=gpu_quota<=4），单点权威。
        配额只约束普通用户（管理员不走配额校验）；对管理员账号改配额无意义，直接拒绝。
        """
        if self.user.role != "admin":
            return False, "仅管理员可修改配额"
        target_name = (inp.get("username") or "").strip()
        if not target_name:
            return False, "参数错误：username 必填"
        try:
            quota = int(inp.get("gpu_quota"))
        except (TypeError, ValueError):
            return False, "参数错误：gpu_quota 必须为整数"
        if not (0 <= quota <= 4):
            return False, "gpu_quota 必须在 0-4 之间"
        target = user_crud.get_user_by_username_ci(self.db, target_name)
        if target is None:
            return False, f"用户不存在：{target_name}"
        if target.role == "admin":
            return False, f"{target.username} 是管理员账号，无需配额"
        user_crud.update_user_quota(self.db, target.id, quota)
        return True, f"已把 {target.username} 的 GPU 配额设为 {quota}"

    def _tool_list_users(self, inp) -> Tuple[bool, str]:
        """管理员专用：枚举所有普通用户（不含管理员），覆盖无容器/无 GPU 分配的用户。

        镜像 /api/admin/users 的口径（role != 'admin'）：普通用户才是可管理的对象，
        配额/改名等管理操作都针对他们。无任何容器的用户（如新注册的测试账号）也照列，
        否则只能靠容器/GPU 结果间接推断用户存在。管理员账号不进列表。
        """
        if self.user.role != "admin":
            return False, "仅管理员可查询用户列表"
        users = self.db.query(models.User).filter(
            models.User.role != "admin"
        ).order_by(models.User.id.asc()).all()
        lines = []
        for u in users:
            used = container_crud.get_user_gpu_used(self.db, u.id)
            total = self.db.query(models.ContainerInstance).filter(
                models.ContainerInstance.user_id == u.id).count()
            running = self.db.query(models.ContainerInstance).filter(
                models.ContainerInstance.user_id == u.id,
                models.ContainerInstance.status == "running").count()
            lines.append(
                f"- username={u.username} mode={u.mode or 'llm'} "
                f"gpu_quota={u.gpu_quota or 0} gpu_used={used} "
                f"containers={total} running={running}"
            )
        return True, "\n".join(lines) if lines else "（没有普通用户）"

    def _tool_delete_user(self, inp) -> Tuple[bool, str]:
        """管理员专用：删除一个普通用户（User management 页删除同口径）。

        镜像 /api/admin/users/{user_id} DELETE + crud.delete_user 的语义：先对该用户
        名下每条 ContainerInstance 记录 stop/remove 对应 docker 容器本体，再清库——
        容器记录、GPU 分配、其创建的镜像、账号全部删除，剩余用户 id 随之重排。
        只清容器与数据库，绝不删宿主机工作区目录（CONTAINER_MOUNT_ROOT/gpu-<username>
        由容器启动时创建、删除用户不动它，同名新用户可复用该工作区）。
        管理员账号不可作删除目标（本工具作用对象与 list_users / User management 一致，
        都是普通用户）。删除不可恢复，须用户本人在当前对话中明确确认后才执行。
        """
        if self.user.role != "admin":
            return False, "仅管理员可删除用户"
        target_name = (inp.get("username") or "").strip()
        if not target_name:
            return False, "参数错误：username 必填"
        target = user_crud.get_user_by_username_ci(self.db, target_name)
        if target is None:
            return False, f"用户不存在：{target_name}"
        if target.role == "admin":
            return False, f"{target.username} 是管理员账号，不能通过删除用户工具删除"
        # 先停/删该用户名下所有 docker 容器（stop/remove 对已不存在容器安全返回）。
        for inst in self.db.query(models.ContainerInstance).filter(
                models.ContainerInstance.user_id == target.id).all():
            docker_runner.stop_container(inst.container_id)
            docker_runner.remove_container(inst.container_id)
        # delete_user 内部多次 commit（含用户 id 重排），此后 target ORM 已过期——
        # 先把要回执的用户名取出，避免回执构造时触达被删对象。
        deleted = target.username
        if not user_crud.delete_user(self.db, target.id):
            return False, f"删除失败：{deleted}"
        return True, (f"已删除用户 {deleted}：其容器已停止/移除，容器记录、GPU 分配、"
                      f"其创建的镜像及账号信息均已清除；宿主机工作区目录已保留。")

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
        block = self._admin_mutation_block()
        if block:
            return block
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

    def _tool_rebuild_container(self, inp) -> Tuple[bool, str]:
        block = self._admin_mutation_block()
        if block:
            return block
        try:
            resp = containers_router._rebuild_container_impl(int(inp["id"]), self.user, self.db, "llm")
            return True, (f"容器已重建 id={resp.id} status={resp.status} port={resp.assigned_port}")
        except HTTPException as e:
            return False, e.detail

    def _tool_create_container(self, inp) -> Tuple[bool, str]:
        block = self._admin_mutation_block()
        if block:
            return block
        try:
            req = schemas.ContainerStartRequest(
                image_id=int(inp["image_id"]),
                gpu_count=int(inp.get("gpu_count", 1)),
                cpu_limit=inp.get("cpu_limit"),
                memory_limit=inp.get("memory_limit"),
                env_vars=inp.get("env_vars") or {},
            )
            resp = containers_router._start_container_impl(req, self.user, self.db, "llm")
            # 访问密码属于敏感信息：不落在 agent 回复/聊天历史/工具回执里，引导去容器列表页查看。
            return True, (f"容器已创建 id={resp.id} image={resp.image} status={resp.status} "
                          f"port={resp.assigned_port}；访问密码不会在此展示，"
                          f"请到『容器列表/详情』页点击复制")
        except HTTPException as e:
            return False, e.detail
        except ValidationError as e:
            return False, str(e)


class ToolCatalog:
    """渐进式披露的运行时视图。

    每轮把 active 工具发全量定义、其余发轻量 stub（name + 一句话 + 空 schema）；
    agent_loop 对第一次调用未激活工具做拦截（不执行），用 activation_text 把完整
    schema 作为 tool_result 回注并 sticky 激活该工具，下一轮起该工具全量声明。
    active 集合只存在于一次 run_agent/run_agent_stream 调用内（单条消息的 ReAct 运行），
    不跨请求持久化 —— schema 从不写入聊天历史，不会随会话累积。
    """

    def __init__(self) -> None:
        self._full: Dict[str, Dict] = {t["name"]: t for t in TOOLS}
        self._summaries: Dict[str, str] = TOOL_SUMMARIES

    def __contains__(self, name: str) -> bool:
        return name in self._full

    def round_specs(self, active: set) -> List[Dict]:
        """active 内的工具发全量、其余发 stub。返回字典只含网关三键，长度=工具总数。"""
        specs = [self._full[n] for n in active if n in self._full]
        specs += [{"name": n, "description": self._summaries[n],
                   "input_schema": _EMPTY_SCHEMA}
                  for n in self._full if n not in active]
        return specs

    def activation_text(self, name: str) -> str:
        """stub 调用被拦截后作为 tool_result 回注的内容：完整 schema + 指示重发。"""
        spec = json.dumps(self._full[name], ensure_ascii=False)
        return ("工具 %s 首次使用，系统已加载其完整参数定义。请忽略本次占位调用，"
                "严格按下面 input_schema 中的参数名与必填项，"
                "重新发起一次带正确参数的真正调用：\n%s" % (name, spec))


CATALOG = ToolCatalog()
