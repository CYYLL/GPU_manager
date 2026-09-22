"""Read bounded facts from a locally available image without running it."""

import io
import re
import tarfile

import docker

from .docker_runner import DockerRunner

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_FILE_BYTES + 64 * 1024
PACKAGE_PATHS = ("/var/lib/dpkg/status", "/lib/apk/db/installed")
INTERESTING = re.compile(
    r"python|cuda|cudnn|torch|tensorflow|jupyter|numpy|scipy|pandas|gcc|node|java|openssl|ssh",
    re.IGNORECASE,
)
SAFE_VALUE = re.compile(r"^[A-Za-z0-9.+_~:/@-]{1,120}$")


def _read_file(container, path):
    try:
        chunks, stat = container.get_archive(path)
    except docker.errors.NotFound:
        return None
    if stat.get("size", 0) > MAX_FILE_BYTES:
        return None
    data = bytearray()
    for chunk in chunks:
        data.extend(chunk)
        if len(data) > MAX_ARCHIVE_BYTES:
            return None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            member = next((m for m in archive if m.isfile()), None)
            if member is None or member.size > MAX_FILE_BYTES:
                return None
            file_obj = archive.extractfile(member)
            return file_obj.read(MAX_FILE_BYTES + 1).decode("utf-8", errors="replace") if file_obj else None
    except (tarfile.TarError, OSError):
        return None


def _os_name(raw):
    if not raw:
        return "未知"
    for key in ("PRETTY_NAME", "NAME"):
        match = re.search(r"^%s=(.+)$" % key, raw, re.MULTILINE)
        if match:
            value = match.group(1).strip().strip('"\'')
            if len(value) <= 120 and "\n" not in value:
                return value
    return "未知"


def _packages(raw, kind):
    if not raw:
        return 0, []
    packages = []
    if kind == "dpkg":
        for block in raw.split("\n\n"):
            fields = dict(re.findall(r"^(Package|Version|Status): ([^\n]+)$", block, re.MULTILINE))
            if fields.get("Status") == "install ok installed":
                packages.append((fields.get("Package", ""), fields.get("Version", "")))
    else:
        for block in raw.split("\n\n"):
            fields = dict(re.findall(r"^([PV]):([^\n]+)$", block, re.MULTILINE))
            if "P" in fields and "V" in fields:
                packages.append((fields["P"], fields["V"]))
    selected = ["%s=%s" % (name, version) for name, version in packages
                if INTERESTING.search(name) and SAFE_VALUE.fullmatch(name)
                and SAFE_VALUE.fullmatch(version)]
    return len(packages), selected[:60]


def inspect_local_image(image_ref):
    runner = DockerRunner()
    if runner.client is None:
        raise RuntimeError("Docker 服务不可用，无法检查镜像")
    try:
        image = runner.client.images.get(image_ref)
    except docker.errors.ImageNotFound as exc:
        raise ValueError("镜像尚未拉取到本机，请联系管理员先拉取") from exc
    attrs = image.attrs
    report = {
        "image": image_ref,
        "id": image.id[:19],
        "os": attrs.get("Os") or "未知",
        "architecture": attrs.get("Architecture") or "未知",
        "size_bytes": attrs.get("Size"),
        "layer_count": len(image.history()),
        "base_os": "未知",
        "package_manager": "未发现 dpkg/apk 记录",
        "package_count": 0,
        "notable_packages": [],
    }
    container = None
    try:
        container = runner.client.containers.create(image=image.id, command="/bin/true",
                                                    network_disabled=True)
        report["base_os"] = _os_name(_read_file(container, "/etc/os-release"))
        for path in PACKAGE_PATHS:
            raw = _read_file(container, path)
            if raw:
                kind = "dpkg" if "dpkg" in path else "apk"
                report["package_manager"] = kind
                report["package_count"], report["notable_packages"] = _packages(raw, kind)
                break
    except docker.errors.DockerException as exc:
        report["filesystem_note"] = "镜像文件系统读取失败：%s" % type(exc).__name__
    finally:
        if container is not None:
            container.remove(force=True)
    return report
