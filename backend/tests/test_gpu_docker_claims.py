from types import SimpleNamespace
from unittest import mock

from app.services.docker_runner import DockerRunner


def test_running_docker_gpu_claims_include_idle_and_unknown_device_requests():
    runner = DockerRunner.__new__(DockerRunner)
    runner.client = mock.Mock()
    runner.client.containers.list.return_value = [
        SimpleNamespace(id="a" * 64, name="specific", attrs={"HostConfig": {
            "DeviceRequests": [{"Capabilities": [["gpu"]], "DeviceIDs": ["2"]}]}}),
        SimpleNamespace(id="b" * 64, name="unknown", attrs={"HostConfig": {
            "DeviceRequests": [{"Capabilities": [["gpu"]], "DeviceIDs": ["GPU-uuid"]}]}}),
        SimpleNamespace(id="c" * 64, name="cpu-only", attrs={"HostConfig": {
            "DeviceRequests": [{"Capabilities": [["compute"]], "DeviceIDs": ["1"]}]}}),
        SimpleNamespace(id="d" * 64, name="nvidia-runtime", attrs={
            "HostConfig": {"Runtime": "nvidia"},
            "Config": {"Env": ["NVIDIA_VISIBLE_DEVICES=1"]}}),
    ]

    claims = runner.get_running_gpu_claims(4)

    assert set(claims) == {0, 1, 2, 3}
    assert {entry["name"] for entry in claims[2]} == {"specific", "unknown"}
    assert {entry["name"] for entry in claims[1]} == {"unknown", "nvidia-runtime"}
