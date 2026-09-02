from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import schemas, models
from ..crud import users as user_crud
from ..crud import containers as container_crud
from ..auth import create_access_token, get_current_user, get_current_admin
from ..services.docker_runner import DockerRunner

router = APIRouter(tags=["users"])

docker_runner = DockerRunner()


@router.post("/api/users/register", response_model=schemas.UserOut)
def register_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = user_crud.get_user_by_username_ci(db, user.username)
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    if len(user.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    return user_crud.create_user(db, user)


@router.post("/api/users/login", response_model=schemas.Token)
def login_user(credentials: schemas.UserLogin, db: Session = Depends(get_db)):
    user = user_crud.authenticate_user(db, credentials.username, credentials.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/api/users/me", response_model=schemas.UserWithUsage)
def read_users_me(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    gpu_used = container_crud.get_user_gpu_used(db, current_user.id)
    user_data = schemas.UserWithUsage.model_validate(current_user)
    user_data.gpu_used = gpu_used
    return user_data


@router.put("/api/users/password", response_model=schemas.Message)
def change_password(
    passwords: schemas.PasswordChange,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Verify old password
    if not current_user.verify_password(passwords.old_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    # Validate new password
    if len(passwords.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    # Hash and save new password
    hashed = user_crud.hash_password(passwords.new_password)
    current_user.hashed_password = hashed
    db.commit()
    return {"message": "Password changed successfully"}


# Admin routes

@router.get("/api/admin/users/check/{username}", response_model=schemas.UserOut)
def admin_check_user(
    username: str,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    user = user_crud.get_user_by_username_ci(db, username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    return user


@router.put("/api/admin/users/reset-password", response_model=schemas.Message)
def admin_reset_password(
    reset: schemas.AdminPasswordReset,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    user = user_crud.get_user_by_username_ci(db, reset.username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    hashed = user_crud.hash_password(reset.new_password)
    user.hashed_password = hashed
    db.commit()
    return {"message": f"Password for '{reset.username}' reset successfully"}


@router.get("/api/admin/users", response_model=List[schemas.UserWithUsage])
def admin_list_users(
    skip: int = 0,
    limit: int = 100,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    # Only return non-admin users
    users = db.query(models.User).filter(models.User.role != "admin").offset(skip).limit(limit).all()
    result = []
    for u in users:
        u_data = schemas.UserWithUsage.model_validate(u)
        u_data.gpu_used = container_crud.get_user_gpu_used(db, u.id)
        result.append(u_data)
    return result


@router.delete("/api/admin/users/{user_id}", response_model=schemas.Message)
def admin_delete_user(
    user_id: int,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    # Prevent deleting the sole admin
    target = user_crud.get_user_by_id(db, user_id)
    if target and target.role == "admin":
        admin_count = db.query(models.User).filter(models.User.role == "admin").count()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the only admin account")

    # Stop and remove all Docker containers belonging to this user
    containers = db.query(models.ContainerInstance).filter(
        models.ContainerInstance.user_id == user_id
    ).all()
    for inst in containers:
        docker_runner.stop_container(inst.container_id)
        docker_runner.remove_container(inst.container_id)

    success = user_crud.delete_user(db, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "User deleted successfully"}


@router.put("/api/admin/users/{user_id}/quota", response_model=schemas.UserOut)
def admin_set_quota(
    user_id: int,
    quota: schemas.QuotaUpdate,
    current_user: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    user = user_crud.update_user_quota(db, user_id, quota.gpu_quota)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
