from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..database import get_db
from .. import models
from ..auth import get_current_user

router = APIRouter(tags=["mode"])


class ModeUpdate(BaseModel):
    mode: str  # "llm" | "traditional"


@router.get("/api/mode")
def get_mode(current_user: models.User = Depends(get_current_user)):
    return {"mode": current_user.mode}


@router.put("/api/mode")
def set_mode(req: ModeUpdate, current_user: models.User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    if req.mode not in ("llm", "traditional"):
        raise HTTPException(status_code=400, detail="mode must be 'llm' or 'traditional'")
    current_user.mode = req.mode
    db.commit()
    return {"mode": current_user.mode}


def require_llm_mode(current_user: models.User = Depends(get_current_user)):
    """Dependency for agent/monitor routes — only llm-mode users may access."""
    if current_user.mode != "llm":
        raise HTTPException(status_code=403, detail="This endpoint requires LLM mode")
    return None
