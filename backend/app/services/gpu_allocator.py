import threading
from contextlib import contextmanager
from sqlalchemy.orm import Session
from typing import List, Tuple
from .. import crud

# Global lock to serialize GPU allocation requests.
# Single-process uvicorn ensures this serializes all concurrent allocations,
# preventing TOCTOU race conditions between checking and reserving GPUs.
_allocation_lock = threading.Lock()


class GPUAllocator:
    """
    Manages GPU allocation lifecycle.
    - Checks quota before allocation
    - Finds available GPUs
    - Reserves and releases GPUs
    """

    def __init__(self, total_gpu_count: int):
        self.total_gpu_count = total_gpu_count

    @contextmanager
    def allocate_guard(self, timeout: float = 30):
        """Acquire the global allocation lock.

        Blocks until the lock is acquired or *timeout* seconds elapse.
        Raises TimeoutError if another allocation is still in progress.
        """
        acquired = _allocation_lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(
                "GPU allocation is busy. Another user is currently allocating GPUs. "
                "Please try again later."
            )
        try:
            yield
        finally:
            _allocation_lock.release()

    def find_available_gpus(self, db: Session, count: int, busy_gpu_ids: List[int] = None) -> List[int]:
        """Find N unallocated GPU IDs.

        Excludes both DB-allocated GPUs and GPUs that show active utilization
        (high memory/GPU usage from non-system processes).
        """
        allocated_ids = set(crud.containers.get_allocated_gpu_ids(db))
        # Also exclude GPUs with active utilization (no DB record but in use)
        if busy_gpu_ids:
            allocated_ids |= set(busy_gpu_ids)
        all_ids = set(range(self.total_gpu_count))
        available = sorted(all_ids - allocated_ids)
        return available[:count]

    def check_quota(self, db: Session, user_id: int, requested: int, role: str = "user") -> Tuple[bool, str]:
        """Check if user has quota for requested GPUs."""
        # Admin users are not limited by quota
        if role == "admin":
            return True, ""
        from ..crud.users import get_user_by_id
        from ..crud.containers import get_user_gpu_used
        user = get_user_by_id(db, user_id)
        if not user:
            return False, "User not found"
        used = get_user_gpu_used(db, user_id)
        if used + requested > user.gpu_quota:
            return False, (
                f"GPU quota exceeded: used {used}, requested {requested}, "
                f"quota {user.gpu_quota}"
            )
        return True, ""
