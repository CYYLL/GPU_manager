from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
from .. import models, schemas
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_user_by_username(db: Session, username: str):
    return db.query(models.User).filter(models.User.username == username).first()


def get_user_by_username_ci(db: Session, username: str):
    """Case-insensitive username lookup."""
    return db.query(models.User).filter(
        func.lower(models.User.username) == func.lower(username)
    ).first()


def get_user_by_id(db: Session, user_id: int):
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_users(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.User).offset(skip).limit(limit).all()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_user(db: Session, user: schemas.UserCreate):
    hashed_password = pwd_context.hash(user.password)
    has_admin = db.query(models.User).filter(models.User.role == "admin").count() > 0
    db_user = models.User(
        username=user.username,
        hashed_password=hashed_password,
        role="admin" if not has_admin else "user",
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def authenticate_user(db: Session, username: str, password: str):
    user = get_user_by_username(db, username)
    if not user or not user.verify_password(password):
        return None
    if not user.is_active:
        return None
    return user


def update_user_quota(db: Session, user_id: int, gpu_quota: int) -> models.User:
    user = get_user_by_id(db, user_id)
    if not user:
        return None
    user.gpu_quota = gpu_quota
    db.commit()
    db.refresh(user)
    return user


def delete_user(db: Session, user_id: int) -> bool:
    """Hard delete a user, release GPU allocations, remove container records, then renumber remaining user IDs."""
    user = get_user_by_id(db, user_id)
    if not user:
        return False

    now = datetime.utcnow()
    # Release active GPU allocations
    db.query(models.GpuAllocation).filter(
        models.GpuAllocation.user_id == user_id,
        models.GpuAllocation.released_at.is_(None)
    ).update({"released_at": now})
    # Delete allocation history
    db.query(models.GpuAllocation).filter(
        models.GpuAllocation.user_id == user_id
    ).delete()
    # Delete container instances
    db.query(models.ContainerInstance).filter(
        models.ContainerInstance.user_id == user_id
    ).delete()
    # Delete gpu_images created by this user
    db.query(models.GpuImage).filter(
        models.GpuImage.created_by == user_id
    ).delete()
    # Delete the user
    db.delete(user)
    db.commit()

    # Renumber remaining users sequentially
    remaining = db.query(models.User).order_by(models.User.id).all()
    for new_id, u in enumerate(remaining, start=1):
        old_id = u.id
        if old_id == new_id:
            continue
        # Update FK references before changing user ID
        db.query(models.ContainerInstance).filter(
            models.ContainerInstance.user_id == old_id
        ).update({"user_id": new_id})
        db.query(models.GpuAllocation).filter(
            models.GpuAllocation.user_id == old_id
        ).update({"user_id": new_id})
        db.query(models.GpuImage).filter(
            models.GpuImage.created_by == old_id
        ).update({"created_by": new_id})
        # Update user ID
        db.query(models.User).filter(models.User.id == old_id).update({"id": new_id})

    db.commit()
    return True
