from sqlalchemy import Column, Integer, String, DateTime, Boolean, Float, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from passlib.context import CryptContext
from .database import Base

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="user")  # "user" or "admin"
    gpu_quota = Column(Integer, default=0)  # max GPUs user can allocate (0 = admin must assign)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    mode = Column(String, default="llm")  # "llm" or "traditional"（注册时按 MODE_DEFAULT 覆盖）

    container_instances = relationship("ContainerInstance", back_populates="user")
    gpu_allocations = relationship("GpuAllocation", back_populates="user")

    def verify_password(self, plain_password):
        return pwd_context.verify(plain_password, self.hashed_password)


class GpuImage(Base):
    __tablename__ = "gpu_images"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)  # display name, e.g. "PyTorch 2.1"
    image = Column(String, nullable=False)  # docker image, e.g. "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
    description = Column(Text, default="")
    min_gpu = Column(Integer, default=1)
    recommended_gpu = Column(Integer, default=1)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ContainerInstance(Base):
    __tablename__ = "container_instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    container_id = Column(String, unique=True, index=True, nullable=False)  # Docker container ID
    image = Column(String, nullable=False)
    gpu_ids = Column(JSON, default=list)  # list of GPU device IDs
    gpu_count = Column(Integer, default=1)
    status = Column(String, default="running")  # running, stopped, error
    cpu_limit = Column(Float, nullable=True)  # CPU cores limit
    memory_limit = Column(Integer, nullable=True)  # memory limit in MB
    assigned_port = Column(Integer, nullable=True)  # host port (22000-22999)
    access_password = Column(String, nullable=True)  # container access password
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    env_vars = Column(JSON, default=dict)  # 重建时还原
    cleanup_protected = Column(Boolean, default=False)  # 清理候选排除

    user = relationship("User", back_populates="container_instances")
    gpu_allocations = relationship("GpuAllocation", back_populates="container_instance")


class GpuAllocation(Base):
    __tablename__ = "gpu_allocations"

    id = Column(Integer, primary_key=True, index=True)
    gpu_id = Column(Integer, nullable=False)  # NVML GPU index
    container_instance_id = Column(Integer, ForeignKey("container_instances.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    allocated_at = Column(DateTime, default=datetime.utcnow)
    released_at = Column(DateTime, nullable=True)

    container_instance = relationship("ContainerInstance", back_populates="gpu_allocations")
    user = relationship("User", back_populates="gpu_allocations")


class ContainerEvent(Base):
    __tablename__ = "container_events"

    id = Column(Integer, primary_key=True, index=True)
    container_instance_id = Column(Integer, ForeignKey("container_instances.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    event = Column(String, nullable=False)  # create/start/stop/delete/external_stop/rebuild
    source = Column(String, nullable=False)  # llm/manual/agent
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    container_instance = relationship("ContainerInstance")
    user = relationship("User")


class DiskSnapshot(Base):
    __tablename__ = "disk_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    total_bytes = Column(Integer, nullable=False)
    used_bytes = Column(Integer, nullable=False)
    free_bytes = Column(Integer, nullable=False)
    usage_percent = Column(Float, nullable=False)
    docker_container_reclaimable_bytes = Column(Integer, default=0)
    docker_image_reclaimable_bytes = Column(Integer, default=0)
    docker_build_cache_reclaimable_bytes = Column(Integer, default=0)


class CleanupLog(Base):
    __tablename__ = "cleanup_logs"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    container_instance_id = Column(Integer, ForeignKey("container_instances.id"), index=True, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=True)
    action = Column(String, nullable=False)  # stop/remove/skip
    reason = Column(Text, default="")
    decision_source = Column(String, default="llm")  # llm/rule_fallback
    freed_bytes = Column(Integer, default=0)
    success = Column(Boolean, default=False)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    role = Column(String, nullable=False)  # user/assistant
    content = Column(Text, default="")
    tool_calls = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User")


class AdminAlert(Base):
    __tablename__ = "admin_alerts"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    type = Column(String, default="capacity")
    level = Column(String, default="warning")  # warning/critical
    message = Column(Text, default="")
    meta = Column(JSON, default=dict)
    resolved_at = Column(DateTime, nullable=True)
