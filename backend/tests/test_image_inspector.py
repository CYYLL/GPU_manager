import io
import tarfile
from unittest.mock import Mock

from app.services import image_inspector


def _archive(content, name):
    payload = content.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return [buf.getvalue()], {"size": len(payload)}


def test_inspects_local_image_without_starting_container(monkeypatch):
    container = Mock()
    files = {
        "/etc/os-release": _archive('PRETTY_NAME="Ubuntu 22.04"\n', "os-release"),
        "/var/lib/dpkg/status": _archive(
            "Package: python3\nVersion: 3.10.6\nStatus: install ok installed\n\n"
            "Package: bash\nVersion: 5.1\nStatus: install ok installed\n", "status"),
    }
    container.get_archive.side_effect = lambda path: files[path]
    image = Mock(id="sha256:" + "a" * 64,
                 attrs={"Os": "linux", "Architecture": "amd64", "Size": 1234})
    image.history.return_value = [{"Id": "1"}, {"Id": "2"}]
    client = Mock()
    client.images.get.return_value = image
    client.containers.create.return_value = container
    monkeypatch.setattr(image_inspector, "DockerRunner", lambda: Mock(client=client))

    report = image_inspector.inspect_local_image("ubuntu:22.04")
    assert report["base_os"] == "Ubuntu 22.04"
    assert report["architecture"] == "amd64"
    assert report["layer_count"] == 2
    assert report["package_count"] == 2
    assert report["notable_packages"] == ["python3=3.10.6"]
    client.containers.create.assert_called_once_with(
        image=image.id, command="/bin/true", network_disabled=True)
    container.start.assert_not_called()
    container.remove.assert_called_once_with(force=True)
