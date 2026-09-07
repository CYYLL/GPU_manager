"""Wire-contract guards: dual-mode + cleanup-protection fields reach the frontend."""
from app import models, schemas


def _user(mode="llm", role="user"):
    # id/is_active have no Python-side default (SQLAlchemy applies them on flush),
    # so set them explicitly for a DB-free serialization test.
    return models.User(id=1, username="w", hashed_password="x", role=role,
                       gpu_quota=8, mode=mode, is_active=True)


def _inst(protected=False, status="stopped"):
    return models.ContainerInstance(id=1, user_id=1, container_id="a" * 64, image="x:1",
                                    gpu_ids=[0], gpu_count=1, status=status,
                                    cleanup_protected=protected)


def test_user_out_carries_mode():
    out = schemas.UserWithUsage.model_validate(_user(mode="traditional"))
    assert out.mode == "traditional"
    assert schemas.UserWithUsage.model_validate(_user()).mode == "llm"


def test_container_response_carries_cleanup_protected():
    out = schemas.ContainerResponse.model_validate(_inst(protected=True))
    assert out.cleanup_protected is True
    assert schemas.ContainerResponse.model_validate(_inst()).cleanup_protected is False
