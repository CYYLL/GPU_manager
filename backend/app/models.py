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
