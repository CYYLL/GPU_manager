"""Docker Hub search, selection and pull progress without network or Docker."""

import time
from unittest.mock import Mock

import pytest
import requests

from app.agent import tools as agent_tools
from app.agent.agent_loop import run_agent_stream
from app.agent.llm_client import LLMResult
from app.services import image_pull


def test_search_returns_metadata_and_requires_next_turn(monkeypatch):
    client = Mock()
    client.images.search.return_value = [{"name": "pytorch/pytorch", "description": "PyTorch",
                                           "is_official": False, "star_count": 100}]
    monkeypatch.setattr(image_pull, "DockerRunner", lambda: Mock(client=client))
    meta = Mock()
    meta.json.return_value = {"description": "Official PyTorch images", "pull_count": 50}
    tags = Mock()
    tags.json.return_value = {"results": [{"name": "2.5-cuda12"}]}
    monkeypatch.setattr(image_pull.requests, "get", Mock(side_effect=[meta, tags]))
    started = time.time()
    rows = image_pull.search_images(101, "pytorch")
    assert rows[0]["refs"] == ["pytorch/pytorch:2.5-cuda12"]
    with pytest.raises(ValueError, match="下一条消息"):
        image_pull.start_pull(101, rows[0]["refs"][0], started)


def test_search_network_unreachable(monkeypatch):
    client = Mock()
    client.images.search.return_value = [{"name": "ubuntu", "description": ""}]
    monkeypatch.setattr(image_pull, "DockerRunner", lambda: Mock(client=client))
    monkeypatch.setattr(image_pull.requests, "get",
                        Mock(side_effect=requests.ConnectionError("network unreachable")))
    with pytest.raises(RuntimeError, match="Docker Hub 网络不可达"):
        image_pull.search_images(102, "ubuntu")


def test_pull_tool_denies_normal_user(monkeypatch):
    user = Mock(role="user", id=3)
    executor = agent_tools.ToolExecutor(Mock(), user)
    ok, result = executor._tool_pull_hub_image({"image_ref": "ubuntu:latest"})
    assert not ok and "仅管理员" in result


def test_pull_stream_registers_image_after_success(monkeypatch):
    job_id = "b" * 32
    image_pull._jobs[job_id] = {"id": job_id, "user_id": 103,
                                "image": "ubuntu:latest", "state": "queued",
                                "percent": 0, "message": "等待拉取"}
    client = Mock()
    client.api.pull.return_value = iter([
        {"status": "Downloading", "id": "layer1",
         "progressDetail": {"current": 50, "total": 100}},
        {"status": "Download complete", "id": "layer1",
         "progressDetail": {"current": 100, "total": 100}},
    ])
    monkeypatch.setattr(image_pull, "DockerRunner", lambda: Mock(client=client))
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    monkeypatch.setattr(image_pull, "SessionLocal", lambda: db)
    image_pull._pull(job_id, "ubuntu:latest", 103)
    assert image_pull.get_job(103, job_id)["state"] == "done"
    db.add.assert_called_once()
    db.commit.assert_called_once()
    client.api.pull.assert_called_once_with("ubuntu", tag="latest", stream=True, decode=True)


def test_stream_reports_progress_then_failure_to_llm(monkeypatch):
    calls = []

    class Llm:
        def stream_complete(self, system, messages, tools):
            calls.append(messages)
            if len(calls) == 1:
                yield {"type": "tool_use", "id": "t1", "name": "pull_hub_image", "input": {}}
            else:
                yield {"type": "text", "delta": "Docker Hub 网络不可达"}

    job_id = "a" * 32
    monkeypatch.setattr("app.agent.agent_loop.time.sleep", lambda _: None)
    events = list(run_agent_stream(
        Llm(), "system", [{"role": "user", "content": "拉取"}], [],
        lambda name, inp: (True, "job_id=" + job_id), max_calls=2,
        pull_status=lambda jid: {"state": "error", "percent": 24,
                                 "message": "Docker Hub 网络不可达"},
    ))
    assert any(e["event"] == "tool_progress" and e["percent"] == 24 for e in events)
    assert any(e["event"] == "tool_result" and not e["ok"] for e in events)
    assert events[-1]["reply"] == "Docker Hub 网络不可达"
