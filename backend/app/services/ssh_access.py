"""Set up a password SSH login inside a managed container."""

import crypt


MANAGED_USERNAME = "gpuuser"


def _exec(container, command):
    code, output = container.exec_run(command, user="root")
    return code == 0, output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)


def configure_ssh_login(container, password, preferred_username=None):
    """Return (ok, username, reason). Never report success without usable SSH auth."""
    ready, _ = _exec(container, ["sh", "-c", "command -v sshd >/dev/null 2>&1"])
    if not ready:
        installed, _ = _exec(container, ["sh", "-c",
            "DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server >/dev/null 2>&1"])
        if not installed:
            return False, None, "镜像缺少 SSH 服务，安装失败"

    running = False
    for command in (["service", "ssh", "start"], ["service", "sshd", "start"],
                    ["/etc/init.d/ssh", "start"], ["/etc/init.d/sshd", "start"]):
        running, _ = _exec(container, command)
        if running:
            break
    if not running:
        return False, None, "SSH 服务启动失败"

    valid, config = _exec(container, ["sshd", "-T"])
    if not valid:
        return False, None, "无法检查 SSH 有效配置"
    settings = dict(line.split(None, 1) for line in config.splitlines() if " " in line)
    if settings.get("passwordauthentication") != "yes":
        return False, None, "镜像禁止 SSH 密码认证"

    root_allowed = settings.get("permitrootlogin") == "yes"
    username = preferred_username if preferred_username and preferred_username != "root" else None
    if username is None and root_allowed:
        username = "root"
    if username is None:
        for suffix in ("", *range(2, 100)):
            candidate = f"{MANAGED_USERNAME}{suffix}"
            exists, _ = _exec(container, ["id", "-u", candidate])
            if not exists:
                username = candidate
                break
        if username is None:
            return False, None, "没有可用的容器 SSH 用户名"
    if username != "root":
        exists, _ = _exec(container, ["id", "-u", username])
        if not exists:
            created, _ = _exec(container, ["useradd", "-m", "-s", "/bin/sh", username])
            if not created:
                return False, None, "创建容器 SSH 用户失败"

    password_hash = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    changed, _ = _exec(container, ["usermod", "-p", password_hash, username])
    if not changed:
        return False, None, "设置容器 SSH 用户密码失败"
    return True, username, "SSH 登录已就绪"
