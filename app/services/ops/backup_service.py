from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models.backup_set import BackupSet


def _create_db_backup() -> str:
    from app.blueprints.billing_invoices.routes.admin import _create_backup_file

    return _create_backup_file()


def create_preop_backup(*, reason: str) -> BackupSet:
    retention_days = int(current_app.config.get("BACKUP_RETENTION_DAYS", 30) or 30)
    now = datetime.utcnow()
    backup_path = _create_db_backup()
    backup = BackupSet(
        created_at=now,
        type="preop",
        reason=reason,
        artifact_paths_json={"db": backup_path},
        verify_status="not_checked",
        retention_until=now + timedelta(days=max(retention_days, 1)),
    )
    db.session.add(backup)
    db.session.flush()
    return backup
