"""Storage cleanup engine: candidate filter → constrained LLM choice →
guardrails → docker reclaim → capacity escalation.

Pure decision logic lives here and is unit-tested; docker/LLM/du are injected
(runner / llm_client parameters, module-level workspace_bytes_provider hook).
"""
import os
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from ..crud import containers as crud


def reclaim_breakdown(df: dict) -> Tuple[int, int, int]:
    """(stopped-container writable bytes, dangling-image bytes, build-cache bytes)
    from a `client.df()` payload. Running containers contribute nothing."""
    containers = df.get("Containers") or []
    images = df.get("Images") or []
    builds = df.get("BuildCache") or []
    ctr = sum(c.get("SizeRw") or 0 for c in containers if not c.get("Running"))
    img = sum(i.get("SizeRootFs") or i.get("Size") or 0 for i in images if not i.get("RepoTags"))
    bc = sum(b.get("Size") or 0 for b in builds)
    return ctr, img, bc


def per_container_estimate(df: dict, container_id: str) -> int:
    """Source-level freed estimate for removing a container: its writable layer
    (SizeRw) plus the image rootfs IF that image dangles after removal (no other
    df container references it). Immune to concurrent /amax writes by design."""
    entry = None
    for c in df.get("Containers") or []:
        if c.get("ID") == container_id or c.get("ID", "").startswith(container_id[:12]):
            entry = c
            break
    if entry is None:
        return 0
    rw = entry.get("SizeRw") or 0
    image_id = entry.get("ImageID")
    if not image_id:
        return rw
    others = [c for c in (df.get("Containers") or []) if c is not entry and c.get("ImageID") == image_id]
    if others:
        return rw  # image still in use elsewhere
    for i in df.get("Images") or []:
        if i.get("ID") == image_id:
            return rw + (i.get("SizeRootFs") or i.get("Size") or 0)
    return rw
