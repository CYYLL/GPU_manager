"""LLM-mode chat API: non-streaming + SSE streaming. Guarded by require_llm_mode."""
import json
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Dict

from ..database import get_db, SessionLocal
from .. import models
from ..auth import get_current_user
from .mode import require_llm_mode
from ..agent.llm_client import create_llm_client
from ..agent.agent_loop import run_agent, run_agent_stream
from ..agent.tools import TOOLS, ToolExecutor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

llm_client = create_llm_client()

SYSTEM_PROMPT = (
    "你是 GPU Manager 的容器助手。你可以查询容器/镜像/GPU/磁盘状态，"
    "设置/取消清理保护，以及创建、启动、停止、删除容器。"
    "可见性：普通用户只能看到和操作自己的容器；管理员可以看到所有用户的所有容器"
    "（list_containers / get_container_status 的结果会带 user= 所属用户名），"
    "但操作他人的容器仍需用户明确要求。"
    "删除容器会销毁容器本身且不可恢复（工作区数据保留但容器配置快照会被清除），"
    "删除前必须向用户确认；创建/启动前确认 GPU 与配额。回答用中文；先调用工具拿结果再回答。"
)

CHAT_HISTORY_LIMIT = 200
# 单次对话 agent 工具调用轮数上限（ReAct 循环最多执行这么多次工具往返后强制给最终答复）。
MAX_TOOL_CALLS = 15


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

    logger.info("agent chat user=%d msg=%.100r", current_user.id, req.message)
    history = _history(db, current_user.id)
    messages = history + [{"role": "user", "content": req.message}]

    # read/commit happened above; the LLM + tool round runs outside a txn
    _save(db, current_user.id, "user", req.message)
    executor = ToolExecutor(db, current_user)
    try:
        out = run_agent(llm_client, SYSTEM_PROMPT, messages, TOOLS,
                        executor.run, max_calls=MAX_TOOL_CALLS)
        logger.info("agent chat done user=%d reply_len=%d tools=%d",
                    current_user.id, len(out.get("reply", "")),
                    len(out.get("tool_trace", [])))
    except Exception:
        # Never 500: persist a fallback assistant reply so history never ends
        # with consecutive user turns, then surface it to the caller.
        logger.exception("agent chat: LLM/tool round failed")
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

    logger.info("agent chat/stream user=%d msg=%.100r",
                current_user.id, req.message)
    history = _history(db, current_user.id)
    messages = history + [{"role": "user", "content": req.message}]
    _save(db, current_user.id, "user", req.message)  # committed before the response returns

    user_id = current_user.id

    def event_source():
        # StreamingResponse runs after the request db closes → open a fresh session.
        # The generator must never adopt the request db (it is closed by then).
        db2 = SessionLocal()
        fallback = "模型调用失败，请稍后重试"
        fail_event = json.dumps({"event": "done", "reply": fallback, "tool_trace": []},
                                ensure_ascii=False)
        t0 = time.monotonic()
        try:
            user = db2.query(models.User).filter(models.User.id == user_id).first()
            if user is None:
                _save(db2, user_id, "assistant", fallback)
                yield f"data: {fail_event}\n\n".encode("utf-8")
                return
            # 上面的 SELECT 已自动开启一个读事务。立即 commit 结束它，
            # 否则下面的 LLM 联网等待（可能数十秒/分钟）期间会一直占着该连接
            # 的读锁 —— sqlite 下会把想写库的请求顶成 PENDING，从而堵死全库。
            # commit 后 user 仍附属于 db2，后续工具按需开启的短事务各自立即收尾。
            db2.commit()
            executor = ToolExecutor(db2, user)
            try:
                for ev in run_agent_stream(llm_client, SYSTEM_PROMPT, messages, TOOLS,
                                           executor.run, max_calls=MAX_TOOL_CALLS):
                    if ev["event"] == "done":
                        _save(db2, user_id, "assistant", ev["reply"])
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
            except Exception:
                # keep role alternation: never leave history ending on a user turn
                logger.exception("agent stream: LLM/tool round failed")
                _save(db2, user_id, "assistant", fallback)
                yield f"data: {fail_event}\n\n".encode("utf-8")
        finally:
            logger.info("agent chat/stream end user=%d elapsed=%.1fs",
                        user_id, time.monotonic() - t0)
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


def _llm_selfcheck() -> int:
    """LLM 连通性自检。

    与生产端点走同一套配置/工具:create_llm_client() 读 .env 的
    LLM_PROVIDER/OPENAI_* 或 LLM_*/ANTHROPIC_*,SYSTEM_PROMPT + TOOLS 与
    /api/agent/chat 完全一致。分两段:
      1) 裸调用(无工具):确认 provider 能返回文字 —— 判断“是否接入”；
      2) agent 链路 run_agent(stub 工具,max_calls=2):确认编排不报错。

    退出码:0 = 接入正常;1/2/3 = 异常/空响应/链路失败(便于脚本判断)。
    """
    from app.agent.llm_client import create_llm_client

    client = create_llm_client()
    print("client    :", type(client).__name__)
    print("model     :", client.model)
    print("base_url  :", client.base_url)
    print("stream    :", client._stream)

    print("\n[1/2] 裸调用(无工具, max_tokens=120) ...")
    try:
        res = client.complete(
            "你是测试助手，请简短回答。",
            [{"role": "user", "content": "你好，请只回复四个字：连接正常"}],
            None, max_tokens=120)
    except Exception as exc:  # 401/超时/网络等一律落到这里
        print("[FAIL] 无法接入 LLM(异常): %s: %s" % (type(exc).__name__, exc))
        return 1
    print("  text       :", repr(res.text))
    print("  tool_calls :", res.tool_calls)
    print("  stop_reason:", res.stop_reason)
    if not res.text:
        print("[FAIL] 模型返回 200 但没有任何文字(空响应)。请检查 base_url/model/key。")
        return 2

    print("\n[2/2] agent 链路(run_agent, SYSTEM_PROMPT+TOOLS, stub 工具, max_calls=2) ...")

    def _stub_executor(name, inp):
        print("   [stub tool] %s <- %r (未真正执行)" % (name, inp))
        return True, "(self-check stub: 未真正执行)"

    try:
        out = run_agent(client, SYSTEM_PROMPT,
                        [{"role": "user", "content": "你好，先别调用工具，直接打个招呼"}],
                        TOOLS, _stub_executor, max_calls=2)
    except Exception as exc:
        print("[FAIL] agent 链路异常: %s: %s" % (type(exc).__name__, exc))
        return 3
    print("  reply     :", repr(out["reply"]))
    print("  tool_trace:", out["tool_trace"])

    print("\n[OK] LLM 接入正常: 裸调用返回文字、agent 链路给出 reply。")
    return 0


if __name__ == "__main__":
    # 允许独立运行做连通性自检(python -m app.routers.agent)
    import os
    import sys
    from pathlib import Path

    from dotenv import load_dotenv

    # 项目根 = backend/app/routers/agent.py → 上溯 3 层
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    sys.exit(_llm_selfcheck())
