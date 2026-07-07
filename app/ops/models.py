from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, text

from app.extensions import db


class DurableJob(db.Model):
    __tablename__ = "durable_jobs"

    # SQLite requires the column type to be exactly "INTEGER" for implicit rowid autoincrement.
    # Use a type variant so tests/dev (SQLite) work while keeping BIGINT in Postgres.
    id = db.Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    queue = db.Column(db.String(64), nullable=False, index=True)
    task = db.Column(db.String(128), nullable=False, index=True)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    payload_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    dedupe_key = db.Column(db.String(255), nullable=True, index=True)
    source_event_id = db.Column(db.String(255), nullable=True, index=True)
    provider_request_id = db.Column(db.String(255), nullable=True, index=True)
    idempotency_scope = db.Column(db.String(64), nullable=True, index=True)

    # queued | running | succeeded | failed | cancelled
    status = db.Column(db.String(16), nullable=False, index=True, default="queued")

    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=5)

    run_at = db.Column(db.DateTime, nullable=False, index=True, default=datetime.utcnow)
    locked_at = db.Column(db.DateTime, nullable=True)
    locked_by = db.Column(db.String(128), nullable=True)

    last_error = db.Column(db.Text, nullable=True)
    last_traceback = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    finished_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        Index("ix_durable_jobs_pick", "status", "queue", "run_at"),
        Index(
            "ux_durable_jobs_dedupe_active",
            "queue",
            "task",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
            sqlite_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
    )


class DiskSample(db.Model):
    """
    Disk usage samples for admin graph visualization (replacing simple boolean alerts for history).
    """

    __tablename__ = "disk_samples"

    id = db.Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    mount_label = db.Column(
        db.String(64), nullable=False, index=True
    )  # e.g. uploads, backups, root
    path = db.Column(db.String(512), nullable=False)

    total_bytes = db.Column(db.BigInteger, nullable=False)
    used_bytes = db.Column(db.BigInteger, nullable=False)
    free_bytes = db.Column(db.BigInteger, nullable=False)
    used_pct = db.Column(db.Float, nullable=False)

    sampled_at = db.Column(db.DateTime, nullable=False, index=True, default=datetime.utcnow)

    __table_args__ = (Index("ix_disk_samples_label_time", "mount_label", "sampled_at"),)
