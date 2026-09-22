"""Docker Hub discovery and administrator-initiated image pulls."""

import re
import threading
import time
import uuid

import docker
import requests

from .. import models
from ..database import SessionLocal
from .docker_runner import DockerRunner

HUB = "https://hub.docker.com/v2/repositories"
REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_lock = threading.Lock()
_candidates = {}  # user_id -> (created, expiry, {image_ref: metadata})
_jobs = {}  # job_id -> status; owned by one admin


def _network_error(exc):
    msg = str(exc).lower()
    return any(s in msg for s in (
        "connection", "connect", "timeout", "timed out", "network",
        "name resolution", "no route", "unreachable", "proxy", "tls", "ssl",
    ))


def search_images(user_id, query):
    query = (query or "").strip()
    if not query or len(query) > 100:
        raise ValueError("请提供 1 到 100 字的镜像需求或关键词")
    runner = DockerRunner()
    if runner.client is None:
        raise RuntimeError("Docker 服务不可用，无法查询 Docker Hub")
    try:
        matches = runner.client.images.search(query, limit=5)
    except (docker.errors.DockerException, requests.RequestException) as exc:
        if _network_error(exc):
            raise RuntimeError("Docker Hub 网络不可达，请检查服务器网络、DNS 或代理后重试") from exc
        raise RuntimeError("Docker Hub 镜像搜索失败：%s" % exc) from exc

    found = []
    for item in matches:
        name = item.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", name):
            continue
        namespace, repo = name.split("/", 1) if "/" in name else ("library", name)
        try:
            detail = requests.get("%s/%s/%s/" % (HUB, namespace, repo), timeout=8)
            detail.raise_for_status()
            meta = detail.json()
            tags_resp = requests.get("%s/%s/%s/tags" % (HUB, namespace, repo),
                                     params={"page_size": 10}, timeout=8)
            tags_resp.raise_for_status()
            tags = [t["name"] for t in tags_resp.json().get("results", [])
                    if isinstance(t.get("name"), str) and t["name"]]
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if _network_error(exc) or (status is not None and (status == 429 or status >= 500)):
                raise RuntimeError("Docker Hub 网络不可达，请检查服务器网络、DNS 或代理后重试") from exc
            continue
        refs = ["%s:%s" % (name, tag) for tag in tags[:5]
                if REF.fullmatch("%s:%s" % (name, tag))]
        if not refs:
            continue
        found.append({"name": name, "description": meta.get("description") or item.get("description") or "无简介",
                      "official": bool(item.get("is_official")), "stars": item.get("star_count", 0),
                      "pull_count": meta.get("pull_count"), "refs": refs})
    with _lock:
        _candidates[user_id] = (time.time(), time.time() + 1800,
                                {ref: row for row in found for ref in row["refs"]})
    return found


def _update(job_id, **fields):
    with _lock:
        _jobs[job_id].update(fields)


def _pull(job_id, image_ref, user_id):
    try:
        runner = DockerRunner()
        if runner.client is None:
            raise RuntimeError("Docker 服务不可用，无法拉取镜像")
        repo, tag = image_ref.rsplit(":", 1)
        layers = {}
        for event in runner.client.api.pull(repo, tag=tag, stream=True, decode=True):
            if event.get("error"):
                raise RuntimeError(event["error"])
            layer_id = event.get("id")
            detail = event.get("progressDetail") or {}
            if layer_id and detail.get("total"):
                layers[layer_id] = (detail.get("current", 0), detail["total"])
            total = sum(t for _, t in layers.values())
            current = sum(min(c, t) for c, t in layers.values())
            percent = min(99, round(current * 100 / total)) if total else 0
            _update(job_id, state="pulling", percent=percent,
                    message=(event.get("status") or "正在拉取") + (" " + layer_id if layer_id else ""))
        runner.client.images.get(image_ref)
        db = SessionLocal()
        try:
            db.query(models.HiddenLocalImage).filter_by(image=image_ref).delete()
            if not db.query(models.GpuImage).filter_by(image=image_ref).first():
                with _lock:
                    meta = _candidates.get(user_id, (0, 0, {}))[2].get(image_ref, {})
                db.add(models.GpuImage(name=image_ref, image=image_ref,
                                       description=meta.get("description", ""),
                                       min_gpu=1, recommended_gpu=1, created_by=user_id))
                db.commit()
            else:
                db.commit()
        finally:
            db.close()
        _update(job_id, state="done", percent=100, message="拉取完成，已加入预设镜像")
    except Exception as exc:
        message = ("Docker Hub 网络不可达，请检查服务器网络、DNS 或代理后重试"
                   if _network_error(exc) else "镜像拉取失败：%s" % exc)
        _update(job_id, state="error", message=message)


def start_pull(user_id, image_ref, turn_started_at=None):
    if not REF.fullmatch(image_ref or ""):
        raise ValueError("镜像名称或标签格式不正确")
    with _lock:
        created, expiry, candidates = _candidates.get(user_id, (0, 0, {}))
        if expiry < time.time() or image_ref not in candidates:
            raise ValueError("请先搜索 Docker Hub 候选镜像，并从结果中选择一个完整镜像标签")
        if turn_started_at is not None and created >= turn_started_at:
            raise ValueError("请先向管理员介绍候选镜像，等待管理员在下一条消息中选择后再拉取")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"id": job_id, "user_id": user_id, "image": image_ref,
                         "state": "queued", "percent": 0, "message": "等待拉取"}
    threading.Thread(target=_pull, args=(job_id, image_ref, user_id), daemon=True).start()
    return job_id


def get_job(user_id, job_id):
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job and job["user_id"] == user_id else None
