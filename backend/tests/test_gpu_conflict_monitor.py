from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent.gpu_conflict_monitor import check_gpu_conflicts
from app.database import Base
from app import models


def test_conflict_alert_is_deduplicated_and_resolved_when_access_no_longer_overlaps():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        runner = mock.Mock()
        runner.get_running_gpu_claims.return_value = {3: [
            {"container_id": "a" * 64, "name": "managed"},
            {"container_id": "b" * 64, "name": "external"},
        ]}

        assert len(check_gpu_conflicts(db, runner, 4)) == 1
        assert len(check_gpu_conflicts(db, runner, 4)) == 1
        alerts = db.query(models.AdminAlert).filter_by(type="gpu_conflict").all()
        assert len(alerts) == 1
        assert alerts[0].resolved_at is None
        assert alerts[0].meta["conflicts"][0]["gpu_id"] == 3

        runner.get_running_gpu_claims.return_value = {3: [
            {"container_id": "a" * 64, "name": "managed"}]}
        assert check_gpu_conflicts(db, runner, 4) == []
        db.refresh(alerts[0])
        assert alerts[0].resolved_at is not None
    finally:
        db.close()
        Base.metadata.drop_all(engine)
