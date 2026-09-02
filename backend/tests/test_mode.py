"""Mode API: get/switch, and require_llm_mode guard rejects traditional users."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
import app.routers.mode as mode_router


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _mk_user(db, mode):
    u = models.User(username="frank", hashed_password="x", role="user", gpu_quota=4, mode=mode)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_get_mode(db):
    u = _mk_user(db, "llm")
    assert mode_router.get_mode(u) == {"mode": "llm"}


def test_switch_mode(db):
    u = _mk_user(db, "llm")
    out = mode_router.set_mode(mode_router.ModeUpdate(mode="traditional"), u, db)
    assert out["mode"] == "traditional"
    db.refresh(u)
    assert u.mode == "traditional"


def test_require_llm_mode_allows_llm(db):
    u = _mk_user(db, "llm")
    assert mode_router.require_llm_mode(u) is None


def test_require_llm_mode_rejects_traditional(db):
    u = _mk_user(db, "traditional")
    with pytest.raises(HTTPException) as excinfo:
        mode_router.require_llm_mode(u)
    assert excinfo.value.status_code == 403
