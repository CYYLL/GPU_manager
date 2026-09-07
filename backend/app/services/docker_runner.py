import os
import docker
from docker.types import DeviceRequest, Mount
from typing import List, Dict, Optional, Tuple
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
    ) -> Tuple[str, str]:
        """
        Start a Docker container with GPU access and resource limits.
        Returns (container_id, status).
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

            # Setup SSH access with password
            if ssh_password:
                ssh_ok, ssh_msg = self.setup_ssh(container.id, ssh_password)
                if not ssh_ok:
                    print(f"SSH setup warning for {container.id[:12]}: {ssh_msg}")
            return container.id, container.status

        except docker.errors.ImageNotFound:
            raise Exception(f"Docker image '{image}' not found. Try pulling it first.")
        except docker.errors.APIError as e:
            raise Exception(f"Docker API error: {str(e)}")

    def setup_ssh(self, container_id: str, password: str) -> Tuple[bool, str]:
        """Configure SSH inside container: set root password and start SSH service."""
        if not self.client:
            return False, "Docker client not initialized"
        try:
            container = self.client.containers.get(container_id)

            # Wait for container to be ready for exec
            for attempt in range(30):
                try:
                    code, _ = container.exec_run("true", user="root")
                    if code == 0:
                        break
                except Exception:
                    pass
                if attempt == 29:
                    return False, "Container not ready for exec"
                time.sleep(1)

            # Shell detection
            shell = "bash"
            code, _ = container.exec_run("which bash", user="root")
            if code != 0:
                shell = "sh"

            def _set_pw(pw: str) -> bool:
                """Set root password by writing a SHA512 hash directly to shadow."""
                import crypt as crypt_mod, base64
                salt = crypt_mod.mksalt(crypt_mod.METHOD_SHA512)
                pw_hash = crypt_mod.crypt(pw, salt)
                # Encode hash in base64 to avoid shell interpretation issues
                b64 = base64.b64encode(pw_hash.encode()).decode()
                code, _ = container.exec_run(
                    f'python3 -c "import re,base64; '
                    f'h=base64.b64decode(\'{b64}\').decode(); '
                    f'c=open(\'/etc/shadow\').read(); '
                    f'c=re.sub(\'^root:[^:]*\',\'root:\'+h,c,flags=re.MULTILINE); '
                    f'open(\'/etc/shadow\',\'w\').write(c)"',
                    user="root"
                )
                return code == 0

            def _verify_pw(pw: str) -> bool:
                """Verify password by checking shadow hash with crypt."""
                import crypt as crypt_mod
                code, out = container.exec_run(
                    'python3 -c "import re; print([l.split(\':\')[1] for l in open(\'/etc/shadow\') if l.startswith(\'root:\')][0])"',
                    user="root"
                )
                if code != 0:
                    return False
                stored = out.decode().strip()
                return crypt_mod.crypt(pw, stored) == stored

            # 1. Configure SSH
            for sed_cmd in [
                'sed -i "s/.*PermitRootLogin.*/PermitRootLogin yes/" /etc/ssh/sshd_config',
                'sed -i "s/.*PasswordAuthentication.*/PasswordAuthentication yes/" /etc/ssh/sshd_config',
                'sed -i "s/.*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication yes/" /etc/ssh/sshd_config',
            ]:
                container.exec_run(f'{shell} -c \'{sed_cmd} 2>/dev/null\'', user="root")

            # 2. Start SSH via any available method
            ssh_started = False
            for cmd in [
                "service ssh start",
                "service sshd start",
                "/etc/init.d/ssh start",
                "/etc/init.d/sshd start",
            ]:
                code, _ = container.exec_run(cmd, user="root")
                if code == 0:
                    ssh_started = True
                    break

            if not ssh_started:
                # Try installing openssh-server
                code, _ = container.exec_run(
                    f'{shell} -c \'DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
                    f'apt-get install -y -qq openssh-server >/dev/null 2>&1 && '
                    f'service ssh start\'',
                    user="root",
                )
                if code == 0:
                    ssh_started = True

            # 3. Set password immediately
            _set_pw(password)

            # 4. Wait for container CMD init to finish, then verify and re-set
            time.sleep(10)
            if not _verify_pw(password):
                # Container init may have reset the password, set it again
                _set_pw(password)
                time.sleep(5)
                if not _verify_pw(password):
                    # Last resort: directly write shadow
                    import subprocess
                    h = subprocess.check_output(
                        ['openssl', 'passwd', '-6', password]
                    ).decode().strip()
                    container.exec_run(
                        f'{shell} -c "'
                        f'cp /etc/shadow /etc/shadow.bak && '
                        f'sed \"s|^root:[^:]*|root:{h}|\" /etc/shadow.bak > /etc/shadow && '
                        f'rm /etc/shadow.bak"',
                        user="root"
                    )

            if ssh_started:
                return True, "SSH ready"
            return False, "SSH not available (tried install, may not be Debian/Ubuntu)"
        except Exception as e:
            return False, f"SSH setup error: {str(e)}"

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

    def ensure_ssh(self, container_id: str) -> Tuple[bool, str]:
        """Start SSH service in an existing (restarted) container whose password is already set.
        Skips password setup and long waits — container filesystem is preserved across stop/start."""
        if not self.client:
            return False, "Docker client not initialized"
        try:
            container = self.client.containers.get(container_id)

            # Wait for container to be ready for exec
            for attempt in range(30):
                try:
                    code, _ = container.exec_run("true", user="root")
                    if code == 0:
                        break
                except Exception:
                    pass
                if attempt == 29:
                    return False, "Container not ready for exec"
                time.sleep(1)

            # Shell detection
            shell = "bash"
            code, _ = container.exec_run("which bash", user="root")
            if code != 0:
                shell = "sh"

            # Ensure SSH config (safe to re-run, sshd_config persists across stop/start)
            for sed_cmd in [
                'sed -i "s/.*PermitRootLogin.*/PermitRootLogin yes/" /etc/ssh/sshd_config',
                'sed -i "s/.*PasswordAuthentication.*/PasswordAuthentication yes/" /etc/ssh/sshd_config',
                'sed -i "s/.*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication yes/" /etc/ssh/sshd_config',
            ]:
                container.exec_run(f'{shell} -c \'{sed_cmd} 2>/dev/null\'', user="root")

            # Start SSH via any available method
            for cmd in [
                "service ssh start",
                "service sshd start",
                "/etc/init.d/ssh start",
                "/etc/init.d/sshd start",
            ]:
                code, _ = container.exec_run(cmd, user="root")
                if code == 0:
                    return True, "SSH started"

            # Try installing openssh-server as fallback
            code, _ = container.exec_run(
                f'{shell} -c \'DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
                f'apt-get install -y -qq openssh-server >/dev/null 2>&1 && '
                f'service ssh start\'',
                user="root",
            )
            if code == 0:
                return True, "SSH installed and started"

            return False, "SSH not available"
        except Exception as e:
            return False, f"SSH start error: {str(e)}"

    def start_container_by_id(self, container_id: str, ssh_password: Optional[str] = None) -> Tuple[bool, str]:
        """Start an existing stopped container and ensure SSH is running.
        Does NOT re-set the password — container filesystem (including /etc/shadow) is preserved.
        Returns (success, message)."""
        if not self.client:
            return False, "Docker client not initialized"
        try:
            container = self.client.containers.get(container_id)
            container.start()
            if ssh_password:
                self.ensure_ssh(container.id)
            return True, "running"
        except docker.errors.NotFound:
            return False, "Container not found in Docker"
        except Exception as e:
            return False, f"Error starting container: {str(e)}"

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

    def is_container_running(self, container_id: str) -> bool:
        """Check if a container is currently running."""
        if not self.client:
            return False
        try:
            container = self.client.containers.get(container_id)
            return container.status == "running"
        except docker.errors.NotFound:
            return False
        except Exception as e:
            print(f"Error checking container status: {e}")
            return False

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
