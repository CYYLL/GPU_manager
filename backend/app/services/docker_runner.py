import os
import docker
from docker.types import DeviceRequest, Mount
from typing import List, Dict, Optional, Tuple
from .ssh_access import configure_ssh_login
import time

class DockerRunner:
    """Docker container lifecycle management with GPU and resource limits."""

    def __init__(self):
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:
            print(f"Failed to connect to Docker daemon: {e}")
            self.client = None

    def start_container(
        self,
        image: str,
        name: str,
        gpu_ids: List[int],
        cpu_limit: Optional[float] = None,
        memory_limit: Optional[int] = None,
        env_vars: Optional[Dict] = None,
        ports: Optional[Dict] = None,
        volumes: Optional[List[Mount]] = None,
        ssh_password: Optional[str] = None,
    ) -> Tuple[str, str, Optional[str]]:
        """
        Start a Docker container with GPU access and resource limits.
        Returns (container_id, status, verified SSH username).
        Raises Exception on failure.
        Supported images:
           ✓ Ubuntu-based images with Python installed
           ✓ Debian-based images with Python installed
           ✓ Most CUDA/PyTorch/TensorFlow images derived from Ubuntu
        """
        if not self.client:
            raise Exception("Docker client not initialized")

        # Specify exact GPU devices by ID (cannot combine with count)
        device_request = None
        if gpu_ids:
            device_request = DeviceRequest(
                    device_ids=[str(gid) for gid in gpu_ids],
                    capabilities=[['gpu']],
                )
            

        # Build resource limits
        mem_limit_str = None
        if memory_limit:
            mem_limit_str = f"{memory_limit}m"

        cpu_period = None
        cpu_quota = None
        if cpu_limit:
            cpu_period = 100000  # default period in microseconds
            cpu_quota = int(cpu_limit * cpu_period)

        try:
            container = self.client.containers.run(
                image=image,
                name=name,
                detach=True,
                auto_remove=False,
                environment=env_vars or {},
                ports=ports or {},
                device_requests=[device_request],
                mounts=volumes or [],
                mem_limit=mem_limit_str,
                cpu_period=cpu_period,
                cpu_quota=cpu_quota,
                stdin_open=True,
                tty=True,
                working_dir="/workspace",
                shm_size="24G",
            )

            for _ in range(5):
                container.reload()

                if container.status == "running":
                    break

                if container.status in ("exited", "dead"):
                    logs = container.logs(tail=50).decode(
                        "utf-8",
                        errors="ignore"
                    )

                    raise Exception(
                        f"Container exited immediately.\n{logs}"
                    )
                time.sleep(1)
            else:
                raise Exception(
                    f"Container startup timeout. status={container.status}"
                )

            # An allocated container must have a verified SSH login before it is recorded.
            ssh_username = None
            if ssh_password:
                ssh_ok, ssh_username, ssh_msg = self.setup_ssh(container.id, ssh_password)
                if not ssh_ok:
                    container.remove(force=True)
                    raise RuntimeError(f"容器 SSH 初始化失败：{ssh_msg}")
            container.reload()
            if container.status != "running":
                container.remove(force=True)
                raise RuntimeError("容器初始化后未保持运行，创建失败")
            return container.id, container.status, ssh_username

        except docker.errors.ImageNotFound:
            raise Exception(f"Docker image '{image}' not found. Try pulling it first.")
        except docker.errors.APIError as e:
            raise Exception(f"Docker API error: {str(e)}")

    def setup_ssh(self, container_id: str, password: str,
                  preferred_username: Optional[str] = None) -> Tuple[bool, Optional[str], str]:
        """Select root only when password login is allowed; otherwise create a user."""
        if not self.client:
            return False, None, "Docker client not initialized"
        try:
            container = self.client.containers.get(container_id)
            for attempt in range(30):
                code, _ = container.exec_run(["true"], user="root")
                if code == 0:
                    break
                if attempt == 29:
                    return False, None, "容器尚未准备好执行命令"
                time.sleep(1)
            return configure_ssh_login(container, password, preferred_username)
        except Exception as exc:
            return False, None, f"SSH 初始化异常：{exc}"

    def stop_container(self, container_id: str, attempts=None, timeout=None, retry_delay=None) -> Tuple[bool, str]:
        """Stop a container but do NOT remove it. On failure, retries up to
        `attempts` times with a fixed per-attempt timeout, giving the container's
        process time to exit. Returns (success, message)."""
        if not self.client:
            return False, "Docker client not initialized"

        attempts = int(os.environ.get("DOCKER_STOP_ATTEMPTS", "3")) if attempts is None else attempts
        attempts = max(1, attempts)
        timeout = int(os.environ.get("DOCKER_STOP_TIMEOUT", "10")) if timeout is None else timeout
        timeout = max(1, timeout)
        retry_delay = int(os.environ.get("DOCKER_STOP_RETRY_DELAY", "2")) if retry_delay is None else retry_delay
        retry_delay = max(0, retry_delay)

        last_err = ""
        for i in range(attempts):
            try:
                container = self.client.containers.get(container_id)
                container.stop(timeout=timeout)
                return True, "stopped"
            except docker.errors.NotFound:
                # Container already gone — no point retrying
                return False, "Container not found in Docker"
            except Exception as e:
                last_err = str(e)
                if i < attempts - 1:
                    time.sleep(retry_delay * (i + 1))
        return False, f"Error stopping container after {attempts} attempts: {last_err}"

    def ensure_ssh(self, container_id: str, password: str,
                   preferred_username: Optional[str] = None) -> Tuple[bool, Optional[str], str]:
        """Recheck SSH after a container restart or repair an older container."""
        return self.setup_ssh(container_id, password, preferred_username)

    def start_container_by_id(self, container_id: str, ssh_password: Optional[str] = None,
                              ssh_username: Optional[str] = None) -> Tuple[bool, str]:
        if not self.client:
            return False, "Docker client not initialized"
        try:
            container = self.client.containers.get(container_id)
            container.start()
            if ssh_password:
                ok, username, reason = self.ensure_ssh(container.id, ssh_password, ssh_username)
                if not ok:
                    container.stop(timeout=10)
                    return False, reason
                container.reload()
                if container.status != "running":
                    return False, "容器启动后未保持运行"
                return True, username or "root"
            container.reload()
            if container.status != "running":
                return False, "容器启动后未保持运行"
            return True, "running"
        except docker.errors.NotFound:
            return False, "Container not found in Docker"
        except Exception as exc:
            return False, f"Error starting container: {exc}"

    def list_containers(self, all: bool = True) -> List[Dict]:
        """List all Docker containers."""
        if not self.client:
            return []
        containers = self.client.containers.list(all=all)
        result = []
        for c in containers:
            result.append({
                "id": c.id[:12],
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else "unknown",
                "status": c.status,
            })
        return result

    def is_container_running(self, container_id: str) -> Optional[bool]:
        """Return None when Docker cannot determine the state."""
        if not self.client:
            return None
        try:
            container = self.client.containers.get(container_id)
            if container.status == "running":
                return True
            if container.status in ("exited", "dead"):
                return False
            return None
        except docker.errors.NotFound:
            return False
        except Exception as e:
            print(f"Error checking container status: {e}")
            return None

    def remove_container(self, container_id: str) -> Tuple[bool, str]:
        """Gracefully stop then remove a Docker container. Stop is retried via
        stop_container(); remove uses force=True so a container that survived the
        failed stop can still be deleted. Returns (success, message)."""
        if not self.client:
            return False, "Docker client not initialized"
        try:
            container = self.client.containers.get(container_id)
            # 先优雅重试停止（给进程时间响应 SIGTERM）
            self.stop_container(container_id)
            # 无论 stop 是否成功都用 force 删除——stop 失败但容器还在运行时，
            # 普通 remove() 会因"容器仍在运行"报错
            container.remove(force=True)
            return True, "removed"
        except docker.errors.NotFound:
            return True, "already removed"
        except Exception as e:
            return False, f"Error removing container: {str(e)}"

    def get_container_logs(self, container_id: str, lines: int = 100) -> str:
        if not self.client:
            return ""
        try:
            container = self.client.containers.get(container_id)
            return container.logs(tail=lines).decode('utf-8')
        except docker.errors.NotFound:
            return "Container not found"
        except Exception as e:
            return f"Error: {str(e)}"

    def df(self) -> dict:
        """docker system df payload (containers/images/build cache sizes)."""
        if not self.client:
            return {}
        try:
            return self.client.df()
        except Exception as e:
            print(f"docker df error: {e}")
            return {}

    def image_prune(self) -> int:
        """Prune dangling images once after a cleanup round. Returns bytes freed."""
        if not self.client:
            return 0
        try:
            res = self.client.images.prune()
            return int(res.get("SpaceReclaimed", 0))
        except Exception as e:
            print(f"docker image prune error: {e}")
            return 0

    def build_cache_prune(self) -> int:
        """Prune build cache only (independent of container removal). Returns bytes freed."""
        if not self.client:
            return 0
        try:
            res = self.client.api.prune_builds()
            return int(res.get("SpaceReclaimed", 0))
        except Exception as e:
            print(f"docker builder prune error: {e}")
            return 0
