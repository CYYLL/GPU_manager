"""LLM-mode chat API. Guarded by require_llm_mode. Non-streaming (Phase 2a)."""
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Dict

from ..database import get_db, SessionLocal
from .. import models
from ..auth import get_current_user
from .mode import require_llm_mode
from ..agent.llm_client import LLMClient
from ..agent.agent_loop import run_agent, run_agent_stream
from ..agent.tools import TOOLS, ToolExecutor

router = APIRouter(tags=["agent"])

llm_client = LLMClient()

SYSTEM_PROMPT = (
    "你是 GPU Manager 的容器助手。你可以查询用户自己的容器/镜像/GPU/磁盘状态，"
    "设置/取消清理保护，以及创建、启动、停止、删除用户的容器。"
    "安全规则：只操作用户自己的资源（除非你是 admin 且用户明确要求）；"
    "删除容器会销毁容器本身且不可恢复（工作区数据保留但容器配置快照会被清除），"
    "删除前必须向用户确认；创建/启动前确认 GPU 与配额。回答用中文；先调用工具拿结果再回答。"
)

CHAT_HISTORY_LIMIT = 200


class ChatRequest(BaseModel):
    message: str


def _history(db: Session, user_id: int, limit: int = 20) -> List[Dict]:
    rows = db.query(models.ChatMessage).filter(
        models.ChatMessage.user_id == user_id
    ).order_by(models.ChatMessage.created_at.desc()).limit(limit).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


def _save(db: Session, user_id: int, role: str, content: str):
    db.add(models.ChatMessage(user_id=user_id, role=role, content=content))
    # enforce per-user cap: drop oldest
    over = db.query(models.ChatMessage).filter(
        models.ChatMessage.user_id == user_id
    ).count()
    if over > CHAT_HISTORY_LIMIT:
        oldest = db.query(models.ChatMessage).filter(
            models.ChatMessage.user_id == user_id
        ).order_by(models.ChatMessage.created_at.asc()).first()
        if oldest:
            db.delete(oldest)
    db.commit()


@router.post("/api/agent/chat")
def chat(req: ChatRequest,
         current_user: models.User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    require_llm_mode(current_user)  # 403 for traditional mode

    history = _history(db, current_user.id)
    messages = history + [{"role": "user", "content": req.message}]

    # read/commit happened above; the LLM + tool round runs outside a txn
    _save(db, current_user.id, "user", req.message)
    executor = ToolExecutor(db, current_user)
    try:
        out = run_agent(llm_client, SYSTEM_PROMPT, messages, TOOLS,
                        executor.run, max_calls=5)
    except Exception:
        # Never 500: persist a fallback assistant reply so history never ends
        # with consecutive user turns, then surface it to the caller.
        fallback = "模型调用失败，请稍后重试"
        _save(db, current_user.id, "assistant", fallback)
        return {"reply": fallback, "tool_trace": []}
    _save(db, current_user.id, "assistant", out["reply"])
    return out


@router.post("/api/agent/chat/stream")
def chat_stream(req: ChatRequest,
                current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    require_llm_mode(current_user)  # 403 for traditional

    history = _history(db, current_user.id)
    messages = history + [{"role": "user", "content": req.message}]
    _save(db, current_user.id, "user", req.message)  # committed before the response returns

    user_id = current_user.id

    def event_source():
        # StreamingResponse runs after the request db closes → open a fresh session
        db2 = SessionLocal()
        try:
            user = db2.query(models.User).filter(models.User.id == user_id).first()
            executor = ToolExecutor(db2, user)
            full_reply = ""
            for ev in run_agent_stream(llm_client, SYSTEM_PROMPT, messages, TOOLS,
                                       executor.run, max_calls=5):
                if ev["event"] == "text":
                    full_reply += ev["delta"]
                if ev["event"] == "done":
                    _save(db2, user_id, "assistant", ev["reply"])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
        finally:
            db2.close()

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/api/agent/session")
def get_session(current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    require_llm_mode(current_user)
    return {"messages": _history(db, current_user.id, CHAT_HISTORY_LIMIT)}


@router.delete("/api/agent/session")
def clear_session(current_user: models.User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    require_llm_mode(current_user)
    db.query(models.ChatMessage).filter(
        models.ChatMessage.user_id == current_user.id
    ).delete()
    db.commit()
    return {"status": "cleared"}
