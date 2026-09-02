import pynvml
from typing import List, Dict, Optional

_instance = None


def get_gpu_monitor() -> "GPUMonitor":
    """Return the singleton GPUMonitor instance."""
    global _instance
    if _instance is None:
        _instance = GPUMonitor()
    return _instance


class GPUMonitor:
    """Read-only NVML GPU status queries. Use get_gpu_monitor() to access singleton."""

    def __init__(self):
        try:
            pynvml.nvmlInit()
            self.initialized = True
        except pynvml.NVMLError as e:
            print(f"Failed to initialize NVML: {e}")
            self.initialized = False

    def get_gpu_count(self) -> int:
        if not self.initialized:
            return 0
        try:
            return pynvml.nvmlDeviceGetCount()
        except pynvml.NVMLError as e:
            print(f"Failed to get GPU count: {e}")
            return 0

    def get_gpu_status(self, allocated_gpu_ids: List[int] = None,
                       allocation_users: Dict[int, str] = None) -> List[Dict]:
        """
        Get all GPU status from NVML.
        :param allocated_gpu_ids: list of GPU ids that are DB-allocated
        :param allocation_users: mapping of gpu_id -> username
        """
        if not self.initialized:
            return []

        if allocated_gpu_ids is None:
            allocated_gpu_ids = []
        if allocation_users is None:
            allocation_users = {}

        gpus = []
        try:
            device_count = pynvml.nvmlDeviceGetCount()
        except pynvml.NVMLError as e:
            print(f"Failed to get GPU count: {e}")
            return gpus

        for i in range(device_count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name_raw = pynvml.nvmlDeviceGetName(handle)
                name = name_raw.decode('utf-8') if isinstance(name_raw, bytes) else name_raw
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                total_memory = mem_info.total // (1024 ** 3)
                used_memory = mem_info.used // (1024 ** 3)
                free_memory = mem_info.free // (1024 ** 3)
                utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)

                try:
                    temperature = pynvml.nvmlDeviceGetTemperature(
                        handle, pynvml.NVML_TEMPERATURE_GPU
                    )
                except pynvml.NVMLError:
                    temperature = 0

                gpu_info = {
                    "id": i,
                    "name": name,
                    "total_memory": total_memory,
                    "used_memory": used_memory,
                    "free_memory": free_memory,
                    "memory_utilization": round(
                        (used_memory / total_memory * 100) if total_memory > 0 else 0, 2
                    ),
                    "gpu_utilization": utilization.gpu,
                    "temperature": temperature,
                    "allocated": i in allocated_gpu_ids,
                    "allocated_to": allocation_users.get(i),
                }
                gpus.append(gpu_info)
            except pynvml.NVMLError as e:
                print(f"GPU {i} query failed (skipping): {e}")
                # Mark failed GPU as unavailable rather than omitting it
                gpus.append({
                    "id": i,
                    "name": "Unavailable",
                    "total_memory": 0,
                    "used_memory": 0,
                    "free_memory": 0,
                    "memory_utilization": 0,
                    "gpu_utilization": 0,
                    "temperature": 0,
                    "allocated": i in allocated_gpu_ids,
                    "allocated_to": allocation_users.get(i),
                    "status": "error",
                    "error": str(e),
                })

        return gpus
