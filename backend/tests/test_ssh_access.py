from app.services.ssh_access import configure_ssh_login


class Container:
    def __init__(self, root_allowed=False, password_allowed=True):
        self.commands = []
        self.users = {"root"}
        self.root_allowed = root_allowed
        self.password_allowed = password_allowed

    def exec_run(self, command, user):
        assert user == "root"
        self.commands.append(command)
        if command == ["sshd", "-T"]:
            return 0, (f"permitrootlogin {'yes' if self.root_allowed else 'without-password'}\n"
                       f"passwordauthentication {'yes' if self.password_allowed else 'no'}\n").encode()
        if command[:2] == ["id", "-u"]:
            return (0 if command[2] in self.users else 1), b""
        if command[0] == "useradd":
            self.users.add(command[-1])
        return 0, b""


def test_root_password_login_uses_root():
    container = Container(root_allowed=True)
    ok, username, _ = configure_ssh_login(container, "secret")
    assert ok and username == "root"
    assert not any(cmd[0] == "useradd" for cmd in container.commands)
    assert any(cmd[0] == "usermod" and cmd[-1] == "root" for cmd in container.commands)


def test_root_prohibited_creates_managed_user():
    container = Container()
    ok, username, _ = configure_ssh_login(container, "secret")
    assert ok and username == "gpuuser"
    assert "gpuuser" in container.users
    assert any(cmd[0] == "usermod" and cmd[-1] == "gpuuser" for cmd in container.commands)


def test_existing_image_user_is_not_taken_over():
    container = Container()
    container.users.add("gpuuser")
    ok, username, _ = configure_ssh_login(container, "secret")
    assert ok and username == "gpuuser2"


def test_password_auth_disabled_rejects():
    container = Container(password_allowed=False)
    ok, username, _ = configure_ssh_login(container, "secret")
    assert not ok and username is None
    assert not any(cmd[0] in ("useradd", "usermod") for cmd in container.commands)
