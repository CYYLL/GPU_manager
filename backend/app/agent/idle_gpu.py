"""闲置 GPU 自动停止策略：每 30 分钟扫描一次，持续满窗口（默认 8h）空闲即停容器。

判定口径：某 running 容器所占用（GpuAllocation / instance.gpu_ids）的每一张卡，
在当次 NVML 采样里同时满足「gpu_utilization == 0 且 memory_utilization ≤ 阈值」
才记为一个"空闲 tick"；任一卡忙碌或采样缺失即视为不确定，绝不误停。

持续空闲时长用 ContainerIdleState 行持久化（空闲开始时刻 idle_since），跨周期、
跨服务重启都成立。满窗口时二次确认（DB 状态 + 新鲜 NVML 采样）后停容器：
docker stop（保留容器）→ 释放分配 → 置 stopped → 记事件 → 向所属用户的 Agent
会话插入一条系统通知。

scan_idle_gpus 是纯函数、可测；守护线程在 main.py 里启动（与 monitor/cleanup 同款）。
环境变量：IDLE_GPU_ENABLED / IDLE_GPU_DRY_RUN / IDLE_GPU_INTERVAL_SECONDS /
IDLE_GPU_HOURS / IDLE_GPU_MEM_IDLE_PCT。
"""
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy.orm import Session

from .. import models
from ..crud import containers as container_crud
from ..services.docker_runner import DockerRunner


@dataclass
class IdleGpuParams:
    """策略参数（env 驱动，控制循环每轮重读）。"""
    enabled: bool = True
    dry_run: bool = False
    idle_hours: float = 8.0            # 持续空闲多久自动停
    memory_idle_pct: float = 5.0       # 显存占用低于该 % 才视为空闲（与 GPU 看板 free 口径一致）

    @classmethod
    def from_env(cls) -> "IdleGpuParams":
        e = os.environ.get
        return cls(
            enabled=e("IDLE_GPU_ENABLED", "true").lower() not in ("0", "false", "no"),
            dry_run=e("IDLE_GPU_DRY_RUN", "false").lower() in ("1", "true", "yes"),
            idle_hours=float(e("IDLE_GPU_HOURS", "8")),
            memory_idle_pct=float(e("IDLE_GPU_MEM_IDLE_PCT", "5")),
        )


def _interval_seconds() -> int:
    return int(os.environ.get("IDLE_GPU_INTERVAL_SECONDS", "1800"))


def _card_idle(s: dict, memory_idle_pct: float) -> bool:
    """单卡空闲判定：利用率 == 0 且显存占用 ≤ 阈值。error 卡视为不空闲。"""
    if s.get("error"):
        return False
    return (int(s.get("gpu_utilization") or 0) == 0
            and float(s.get("memory_utilization") or 0) <= memory_idle_pct)


def _all_idle_or_none(sample: Dict[int, dict], gpu_ids, memory_idle_pct: float) -> Optional[bool]:
    """容器整组卡的空闲判定：全空闲 True；任一忙碌 False；无 GPU / 采样缺失 None
    （None 表示本周期无法下结论，既不延展也不清空标记，绝不据此停容器）。"""
    if not gpu_ids:
        return None
    for gid in gpu_ids:
        s = sample.get(gid)
        if s is None:
            return None
        if not _card_idle(s, memory_idle_pct):
            return False
    return True


def _append_notice(db: Session, user_id: int, content: str):
    """向该用户 Agent 会话追加一条 assistant 系统通知（限长同聊天历史上限）。"""
    db.add(models.ChatMessage(user_id=user_id, role="assistant", content=content))
    limit = int(os.environ.get("CHAT_HISTORY_LIMIT", "200"))
    total = db.query(models.ChatMessage).filter(
        models.ChatMessage.user_id == user_id).count()
    if total > limit:
        oldest = db.query(models.ChatMessage).filter(
            models.ChatMessage.user_id == user_id
        ).order_by(models.ChatMessage.created_at.asc()).first()
        if oldest:
            db.delete(oldest)
    db.commit()


def scan_idle_gpus(db: Session, gpu_monitor, docker_runner, now: datetime = None,
                   p: IdleGpuParams = None) -> dict:
    """一轮闲置 GPU 扫描。返回 {"status", "checked", "idle_starts", "busy_reset",
    "planned", "stopped", "notified", "errors"}。外部调用（docker/NVML）从不
    持有打开的 DB 事务：状态标记先落库、再逐个动作。"""
    now = now or datetime.utcnow()
    p = p or IdleGpuParams()
    out = {"status": "ok", "checked": 0, "idle_starts": 0, "busy_reset": 0,
           "planned": 0, "stopped": 0, "notified": 0, "errors": []}
    if not p.enabled:
        out["status"] = "disabled"
        return out

    try:
        statuses = gpu_monitor.get_gpu_status()
    except Exception as e:  # NVML/读取异常 → 本周期不动任何容器
        out.update(status="error", errors=[str(e)])
        return out
    if not statuses:  # NVML 未初始化 / 无卡可读 → 不判定空闲（防空转误停）
        out["status"] = "unreadable"
        return out
    sample = {s["id"]: s for s in statuses}

    running = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.status == "running").all()
    running_ids = {i.id for i in running}

    # 清理不再运行容器的残留标记（容器被删/置 stopped 但未走本策略）。
    orphan_q = db.query(models.ContainerIdleState)
    if running_ids:
        orphan_q = orphan_q.filter(
            models.ContainerIdleState.container_instance_id.notin_(running_ids))
    orphan_q.delete(synchronize_session=False)

    states = {r.container_instance_id: r
              for r in db.query(models.ContainerIdleState).all()}
    candidates = []

    for inst in running:
        idle = _all_idle_or_none(sample, inst.gpu_ids or [], p.memory_idle_pct)
        if idle is None:
            continue  # 本周期无法下结论
        out["checked"] += 1
        row = states.get(inst.id)
        if idle:
            if row is None:
                # 忙碌 → 空闲的第一次：记录空闲起点（不落库先，批量后 commit）
                db.add(models.ContainerIdleState(
                    container_instance_id=inst.id, idle_since=now))
                out["idle_starts"] += 1
                continue
            hours_idle = (now - row.idle_since).total_seconds() / 3600.0
            if hours_idle >= p.idle_hours:
                candidates.append((inst, row))
        else:
            if row is not None:
                db.delete(row)
                out["busy_reset"] += 1

    db.commit()  # 状态标记落库后再做外部调用

    for inst, row in candidates:
        inst2 = db.query(models.ContainerInstance).filter(
            models.ContainerInstance.id == inst.id).first()
        if inst2 is None or inst2.status != "running":
            db.delete(row)
            db.commit()
            continue
        # 动作前二次确认：此刻仍整组空闲（防 8h 判定后用户恰好开始跑任务）。
        try:
            fresh = {s["id"]: s for s in gpu_monitor.get_gpu_status()}
        except Exception:
            continue
        if _all_idle_or_none(fresh, inst2.gpu_ids or [], p.memory_idle_pct) is not True:
            db.delete(row)  # 已恢复使用 → 清标记下轮重新累计
            db.commit()
            continue
        if p.dry_run:
            out["planned"] += 1
            continue
        # 先抢占/删除标记行：跨进程并发时只有删到行的那个执行停容器，避免双停、
        # 双通知。停失败则下一周期从零重计（安全，不做热重试）。
        claimed = db.query(models.ContainerIdleState).filter(
            models.ContainerIdleState.container_instance_id == inst2.id).delete()
        db.commit()
        if not claimed:
            continue

        ok, msg = docker_runner.stop_container(inst2.container_id)
        if not ok and docker_runner.is_container_running(inst2.container_id) is not False:
            out["errors"].append(f"id={inst2.id}: {msg}")
            continue
        container_crud.release_allocations_by_container(db, inst2.id)
        container_crud.stop_container_instance(db, inst2.id)
        container_crud.record_container_event(
            db, inst2.id, inst2.user_id, "stop", source="agent",
            detail=f"GPU 利用率连续 {p.idle_hours:g} 小时为 0，自动停止")
        _append_notice(
            db, inst2.user_id,
            f"【系统通知】你的容器 id={inst2.id}（{inst2.image}，cid={inst2.container_id[:12]}）"
            f"因 GPU 利用率连续 {p.idle_hours:g} 小时为 0，已被自动停止；"
            f"如需继续使用，请重启该容器。")
        out["stopped"] += 1
        out["notified"] += 1

    return out


def _loop_once():
    """在独立 session 里跑一轮；异常由线程调用方吞掉（non-fatal）。"""
    from ..database import SessionLocal  # 延后 import 避免模块加载期成环
    db = SessionLocal()
    try:
        from ..services.gpu_monitor import get_gpu_monitor
        scan_idle_gpus(db, get_gpu_monitor(), DockerRunner(),
                       p=IdleGpuParams.from_env())
    finally:
        db.close()


def start_idle_gpu_thread():
    """守护线程：每 IDLE_GPU_INTERVAL_SECONDS 跑一轮闲置 GPU 扫描。"""
    def _run():
        while True:
            try:
                _loop_once()
            except Exception as e:
                print(f"idle-gpu cycle error (non-fatal): {e}")
            time.sleep(_interval_seconds())
    t = threading.Thread(target=_run, daemon=True, name="gpu-manager-idle-gpu")
    t.start()
    return t
