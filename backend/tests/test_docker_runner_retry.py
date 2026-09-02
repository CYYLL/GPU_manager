"""Unit tests for DockerRunner stop/remove retry logic.

All Docker calls are mocked — never touches a real daemon.
The retry sleep is patched so tests run instantly.
"""
import pytest
from unittest import mock

import docker

from app.services.docker_runner import DockerRunner


def _make_runner(stop_errors=None):
    """Build a DockerRunner bypassing __init__ (no daemon ping) with a mocked client."""
    runner = DockerRunner.__new__(DockerRunner)
    runner.client = mock.Mock()
    container = runner.client.containers.get.return_value
    if stop_errors is not None:
        container.stop.side_effect = stop_errors  # list: sequential raise/return values
    return runner, container


# ── 1. stop_container 重试 ────────────────────────────────────────────────


def test_stop_succeeds_on_first_attempt():
    runner, container = _make_runner()
    ok, msg = runner.stop_container("abc123", attempts=3, timeout=10)
    assert ok is True
    assert msg == "stopped"
    container.stop.assert_called_once_with(timeout=10)


def test_stop_retries_then_succeeds():
    runner, container = _make_runner(stop_errors=[Exception("timeout"), None])
    with mock.patch("app.services.docker_runner.time.sleep"):
        ok, msg = runner.stop_container("abc123", attempts=3, timeout=10, retry_delay=1)
    assert ok is True
    assert container.stop.call_count == 2


def test_stop_fails_after_all_attempts():
    runner, container = _make_runner(
        stop_errors=[Exception("e1"), Exception("e2"), Exception("e3")]
    )
    with mock.patch("app.services.docker_runner.time.sleep"):
        ok, msg = runner.stop_container("abc123", attempts=3, timeout=10, retry_delay=1)
    assert ok is False
    assert container.stop.call_count == 3
    assert "after 3 attempts" in msg


def test_stop_does_not_retry_on_not_found():
    runner = DockerRunner.__new__(DockerRunner)
    runner.client = mock.Mock()
    runner.client.containers.get.side_effect = docker.errors.NotFound("nope")
    ok, msg = runner.stop_container("missing", attempts=3, timeout=10)
    assert ok is False
    assert "not found" in msg.lower()
    runner.client.containers.get.assert_called_once()


def test_stop_honors_env_override(monkeypatch):
    monkeypatch.setenv("DOCKER_STOP_ATTEMPTS", "2")
    monkeypatch.setenv("DOCKER_STOP_TIMEOUT", "5")
    monkeypatch.setenv("DOCKER_STOP_RETRY_DELAY", "0")
    runner, container = _make_runner(
        stop_errors=[Exception("e1"), Exception("e2")]
    )
    ok, msg = runner.stop_container("abc123")
    assert ok is False
    assert "after 2 attempts" in msg
    assert container.stop.call_count == 2
    container.stop.assert_any_call(timeout=5)


# ── 2. remove_container force 兜底 ────────────────────────────────────────


def test_remove_forces_after_stop_retries_exhausted():
    runner, container = _make_runner(
        stop_errors=[Exception("e1"), Exception("e2"), Exception("e3")]
    )
    with mock.patch("app.services.docker_runner.time.sleep"):
        ok, msg = runner.remove_container("abc123")
    assert ok is True
    assert msg == "removed"
    container.remove.assert_called_once_with(force=True)


def test_remove_returns_already_removed_on_not_found():
    runner = DockerRunner.__new__(DockerRunner)
    runner.client = mock.Mock()
    runner.client.containers.get.side_effect = docker.errors.NotFound("nope")
    ok, msg = runner.remove_container("missing")
    assert ok is True
    assert msg == "already removed"


def test_remove_returns_error_when_force_remove_fails():
    runner, container = _make_runner()
    container.remove.side_effect = Exception("remove denied")
    ok, msg = runner.remove_container("abc123")
    assert ok is False
    assert "remove denied" in msg
