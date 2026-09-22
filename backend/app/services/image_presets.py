"""Local image catalog and each user's independent preset selection."""

import logging

from sqlalchemy.exc import IntegrityError

from .. import models
from ..database import SessionLocal

logger = logging.getLogger(__name__)

MIGRATION_KEY = "user_image_presets_v1"


def migrate_legacy_presets():
    """Give existing users their old shared presets once; new users start empty."""
    db = SessionLocal()
    try:
        if db.query(models.PresetMigration).filter_by(key=MIGRATION_KEY).first():
            return
        refs = {row[0] for row in db.query(models.GpuImage.image).all()}
        for (user_id,) in db.query(models.User.id).all():
            for ref in refs:
                db.add(models.UserImagePreset(user_id=user_id, image_ref=ref))
        db.add(models.PresetMigration(key=MIGRATION_KEY))
        db.commit()
    except IntegrityError:
        db.rollback()
        # Another worker may have completed the one-time migration first.
        if not db.query(models.PresetMigration).filter_by(key=MIGRATION_KEY).first():
            raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def local_image_entries(docker_client):
    if docker_client is None:
        raise RuntimeError("Docker 服务不可用，无法查询本地镜像")
    try:
        images = docker_client.images.list()
    except Exception as exc:
        raise RuntimeError("查询 Docker 本地镜像失败：%s" % exc) from exc
    entries = []
    for image in images:
        refs = [ref for ref in (image.tags or []) if ref and ref != "<none>:<none>"]
        for ref in (refs or [image.id]):
            if ref:
                entries.append({"image_ref": ref, "image_id": image.id,
                                "size_bytes": image.attrs.get("Size")})
    return entries


def sync_local_image_presets(db, docker_client, entries=None):
    """Keep the shared catalog current; never change any user's selections."""
    if docker_client is None and entries is None:
        return 0
    if entries is None:
        try:
            entries = local_image_entries(docker_client)
        except Exception:
            logger.exception("Could not list Docker images for preset sync")
            return 0

    existing = {row[0] for row in db.query(models.GpuImage.image).all()}
    added = 0
    for entry in entries:
        ref = entry["image_ref"]
        if ref in existing:
            continue
        db.add(models.GpuImage(name=ref, image=ref, description="",
                               min_gpu=1, recommended_gpu=1))
        existing.add(ref)
        added += 1
    if added:
        db.commit()
    return added


def selected_images(db, user_id):
    refs = {row[0] for row in db.query(models.UserImagePreset.image_ref).filter_by(
        user_id=user_id).all()}
    seen = set()
    result = []
    for image in db.query(models.GpuImage).order_by(models.GpuImage.id).all():
        if image.image in refs and image.image not in seen:
            result.append(image)
            seen.add(image.image)
    return result


def set_selection(db, user_id, image_ref, selected, docker_client):
    ref = image_ref.strip()
    if selected:
        entries = local_image_entries(docker_client)
        available = {entry["image_ref"] for entry in entries}
        if ref not in available:
            raise ValueError("镜像不在 Docker 本地列表中，无法加入预设")
        sync_local_image_presets(db, docker_client, entries)
    row = db.query(models.UserImagePreset).filter_by(user_id=user_id, image_ref=ref).first()
    if selected and row is None:
        db.add(models.UserImagePreset(user_id=user_id, image_ref=ref))
    elif not selected and row is not None:
        db.delete(row)
    db.commit()
