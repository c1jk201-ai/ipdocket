from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Iterable

from flask import current_app
from sqlalchemy import desc, func

from app.extensions import db
from app.models.error_report import ErrorReport
from app.services.core.config_service import ConfigService


@dataclass(frozen=True)
class ErrorReportSummary:
    error_type: str | None
    endpoint: str | None
    message_prefix: str | None
    count: int
    last_seen: datetime | None


def summarize_error_reports(
    *,
    window_minutes: int = 60,
    limit: int = 20,
    min_count: int = 1,
) -> list[ErrorReportSummary]:
    window = max(1, int(window_minutes or 60))
    limit = max(1, int(limit or 20))
    min_count = max(1, int(min_count or 1))

    cutoff = datetime.utcnow() - timedelta(minutes=window)
    message_prefix = func.substr(ErrorReport.message, 1, 220).label("message_prefix")

    rows = (
        db.session.query(
            ErrorReport.error_type.label("error_type"),
            ErrorReport.endpoint.label("endpoint"),
            message_prefix,
            func.count().label("count"),
            func.max(ErrorReport.created_at).label("last_seen"),
        )
        .filter(ErrorReport.created_at >= cutoff)
        .group_by(ErrorReport.error_type, ErrorReport.endpoint, message_prefix)
        .having(func.count() >= min_count)
        .order_by(desc("count"), desc("last_seen"))
        .limit(limit)
        .all()
    )

    return [
        ErrorReportSummary(
            error_type=row.error_type,
            endpoint=row.endpoint,
            message_prefix=row.message_prefix,
            count=int(row.count or 0),
            last_seen=row.last_seen,
        )
        for row in rows
    ]


def count_error_reports(*, window_minutes: int = 60) -> int:
    window = max(1, int(window_minutes or 60))
    cutoff = datetime.utcnow() - timedelta(minutes=window)
    return int(
        db.session.query(func.count(ErrorReport.id))
        .filter(ErrorReport.created_at >= cutoff)
        .filter(ErrorReport.status_code >= 500)
        .scalar()
        or 0
    )


def summarize_error_reports_days(
    *,
    window_days: int = 7,
    limit: int = 20,
    min_count: int = 1,
    swallowed_only: bool = False,
) -> list[ErrorReportSummary]:
    days = max(1, int(window_days or 7))
    limit = max(1, int(limit or 20))
    min_count = max(1, int(min_count or 1))

    cutoff = datetime.utcnow() - timedelta(days=days)
    message_prefix = func.substr(ErrorReport.message, 1, 220).label("message_prefix")

    q = db.session.query(
        ErrorReport.error_type.label("error_type"),
        ErrorReport.endpoint.label("endpoint"),
        message_prefix,
        func.count().label("count"),
        func.max(ErrorReport.created_at).label("last_seen"),
    ).filter(ErrorReport.created_at >= cutoff)

    if swallowed_only:
        q = q.filter(
            (ErrorReport.endpoint.ilike("swallowed:%")) | (ErrorReport.message.ilike("swallowed:%"))
        )

    rows = (
        q.group_by(ErrorReport.error_type, ErrorReport.endpoint, message_prefix)
        .having(func.count() >= min_count)
        .order_by(desc("count"), desc("last_seen"))
        .limit(limit)
        .all()
    )

    return [
        ErrorReportSummary(
            error_type=row.error_type,
            endpoint=row.endpoint,
            message_prefix=row.message_prefix,
            count=int(row.count or 0),
            last_seen=row.last_seen,
        )
        for row in rows
    ]


def fetch_recent_error_reports(*, limit: int = 50) -> list[ErrorReport]:
    return (
        ErrorReport.query.order_by(ErrorReport.created_at.desc())
        .limit(max(1, int(limit or 50)))
        .all()
    )


def _send_error_email(*, subject: str, body: str, recipients: Iterable[str]) -> bool:
    recipients = [r.strip() for r in recipients if r and str(r).strip()]
    if not recipients:
        return False

    smtp_host = current_app.config.get("MAIL_SERVER", "localhost")
    smtp_port = int(current_app.config.get("MAIL_PORT", 587) or 587)
    smtp_user = current_app.config.get("MAIL_USERNAME")
    smtp_pass = current_app.config.get("MAIL_PASSWORD")
    smtp_tls = bool(current_app.config.get("MAIL_USE_TLS", True))
    sender = current_app.config.get("MAIL_DEFAULT_SENDER", smtp_user)
    if not sender:
        return False

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_tls:
                server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(sender, recipients, msg.as_string())
        return True
    except Exception as exc:
        current_app.logger.warning("Error report email failed: %s", exc)
        return False



def _release_db_session_before_alert_io() -> None:
    try:
        db.session.rollback()
    except Exception as exc:
        current_app.logger.debug("Error report alert session rollback skipped: %s", exc)


def _config_int(key: str, default: int) -> int:
    raw = current_app.config.get(key)
    if raw not in (None, ""):
        try:
            return int(raw)
        except Exception:
            return int(default)
    value = ConfigService.get_int(key, default, prefer_env=True)
    return int(default if value is None else value)


def send_error_report_alerts() -> dict[str, object]:
    enabled = bool(current_app.config.get("ERROR_REPORT_ALERTS_ENABLED", False))
    if not enabled:
        return {"enabled": False}

    window_minutes = int(current_app.config.get("ERROR_REPORT_ALERT_WINDOW_MINUTES", 60) or 60)
    threshold = int(current_app.config.get("ERROR_REPORT_ALERT_THRESHOLD", 10) or 10)
    total_threshold = _config_int("ERROR_REPORT_ALERT_TOTAL_THRESHOLD", threshold)
    limit = int(current_app.config.get("ERROR_REPORT_ALERT_LIMIT", 10) or 10)

    summary = summarize_error_reports(
        window_minutes=window_minutes,
        limit=limit,
        min_count=threshold,
    )
    total_count = count_error_reports(window_minutes=window_minutes) if total_threshold > 0 else 0
    if not summary and total_count < total_threshold:
        return {"enabled": True, "candidates": 0, "total_count": total_count, "sent": 0}

    lines = [f"Error spike (last {window_minutes}m):"]
    if total_threshold > 0 and total_count >= total_threshold:
        lines.append(f"- total 5xx reports: {total_count} (threshold {total_threshold})")
    if not summary:
        lines.append("- no single fingerprint crossed the per-error threshold")
    for row in summary:
        endpoint = row.endpoint or "-"
        etype = row.error_type or "Exception"
        msg = (row.message_prefix or "").replace("\n", " ").strip()
        if len(msg) > 180:
            msg = msg[:177] + "..."
        lines.append(f"- {row.count}x {etype} @ {endpoint} :: {msg}")

    text = "\n".join(lines)
    current_app.logger.warning(text)

    _release_db_session_before_alert_io()

    sent = 0
    email_recipients = current_app.config.get("ERROR_REPORT_ALERT_EMAILS") or ""
    email_list = str(email_recipients).split(",")
    if _send_error_email(
        subject=f"[Error Report] {len(summary)} spike(s) detected",
        body=text,
        recipients=email_list,
    ):
        sent += 1

    return {"enabled": True, "candidates": len(summary), "total_count": total_count, "sent": sent}
