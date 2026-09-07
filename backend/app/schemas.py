from pydantic import BaseModel, Field
from typing import Optional, Dict, List, Any
from datetime import datetime


# ── Auth / User ──
class UserCreate(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class UserOut(BaseModel):
    id: int
    username: str
    role: str
    gpu_quota: int
    is_active: bool
    mode: str = "llm"  # "llm" | "traditional"（双模式前端展示）
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class UserWithUsage(UserOut):
    gpu_used: int = 0  # computed, not a column

class Token(BaseModel):
    access_token: str
    token_type: str


# ── GPU Images ──
class GpuImageCreate(BaseModel):
    name: str
    image: str
    description: Optional[str] = ""
    min_gpu: Optional[int] = 1
    recommended_gpu: Optional[int] = 1

class GpuImageOut(BaseModel):
    id: int
    name: str
    image: str
    description: str
    min_gpu: int
    recommended_gpu: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Containers ──
class ContainerStartRequest(BaseModel):
    image_id: int
    gpu_count: int = Field(default=1, ge=1, le=4)
    cpu_limit: Optional[float] = None  # CPU cores
    memory_limit: Optional[int] = None  # MB
    env_vars: Optional[Dict[str, str]] = {}
    ports: Optional[Dict[str, str]] = {}

class ContainerResponse(BaseModel):
    id: int
    container_id: str
    image: str
    status: str
    user_id: int
    gpu_ids: List[int]
    gpu_count: int
    cpu_limit: Optional[float] = None
    memory_limit: Optional[int] = None
    assigned_port: Optional[int] = None
    access_password: Optional[str] = None
    cleanup_protected: bool = False  # 清理候选排除（自动清理引擎/前端保护开关）
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── GPU Status ──
class GPUStatus(BaseModel):
    id: int
    name: str
    total_memory: int       # GB
    used_memory: int        # GB
    free_memory: int        # GB
    memory_utilization: float
    gpu_utilization: int
    temperature: int
    allocated: bool          # whether this GPU is currently allocated
    status: str = "free"     # "free" | "occupied" | "stale" | "error"
    allocated_to: Optional[str] = None  # username if allocated
    error: Optional[str] = None         # error message if GPU is unavailable

class GPUAllocationOut(BaseModel):
    id: int
    gpu_id: int
    username: str
    container_id: str
    allocated_at: datetime
    released_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Admin ──
class QuotaUpdate(BaseModel):
    gpu_quota: int = Field(default=1, ge=0, le=4)

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class AdminPasswordReset(BaseModel):
    username: str
    new_password: str


# ── Generic ──
class Message(BaseModel):
    message: str
