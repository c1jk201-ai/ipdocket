from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app

from app.models.error_report import ErrorReport


def test_logged_exception_is_captured_into_error_reports(db_session):
    from app.utils.error_logging import install_error_report_logging_hook

    current_app.config["ERROR_REPORTING_ENABLED"] = True
    current_app.config["ERROR_REPORT_CAPTURE_LOGGED_EXCEPTIONS"] = True
    install_error_report_logging_hook(current_app)
    assert any(
        h.__class__.__name__ == "_ErrorReportCaptureHandler" for h in current_app.logger.handlers
    )

    db_session.query(ErrorReport).delete(synchronize_session=False)
    db_session.commit()

    try:
        raise RuntimeError("capture-via-logger-exception")
    except Exception:
        current_app.logger.exception("handled error path")

    db_session.expire_all()
    row = ErrorReport.query.order_by(ErrorReport.id.desc()).first()
    assert row is not None
    assert row.error_type == "RuntimeError"
    assert "logged:" in (row.message or "")
    assert "capture-via-logger-exception" in (row.message or "")


def test_admin_error_reports_source_filter_logged(admin_client, db_session):
    db_session.query(ErrorReport).delete(synchronize_session=False)
    db_session.commit()

    now = datetime.utcnow()
    db_session.add_all(
        [
            ErrorReport(
                created_at=now - timedelta(minutes=5),
                method="SYSTEM",
                path="logged:tests.mail",
                endpoint="logged:tests.mail",
                status_code=500,
                error_type="RuntimeError",
                message="logged:tests.mail: smtp failed",
            ),
            ErrorReport(
                created_at=now - timedelta(minutes=4),
                method="SYSTEM",
                path="swallowed:mail.sync",
                endpoint="swallowed:mail.sync",
                status_code=500,
                error_type="ValueError",
                message="swallowed:mail.sync: timeout",
            ),
            ErrorReport(
                created_at=now - timedelta(minutes=3),
                method="POST",
                path="/api/matter/upload",
                endpoint="api.upload",
                status_code=500,
                error_type="OperationalError",
                message="api upload failed",
            ),
        ]
    )
    db_session.commit()

    res = admin_client.get("/admin/errorsNewwindow=120&source=logged&limit=20&recent=20&q=smtp")
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert "logged:tests.mail" in html
    assert "swallowed:mail.sync" not in html
    assert "api.upload" not in html


def test_error_report_alerts_use_total_threshold_without_recipients(app, db_session):
    from app.services.core.config_service import ConfigService
    from app.services.ops import error_report_monitor

    db_session.query(ErrorReport).delete(synchronize_session=False)
    now = datetime.utcnow()
    db_session.add_all(
        [
            ErrorReport(
                created_at=now - timedelta(minutes=5),
                endpoint="api.alpha",
                status_code=500,
                error_type="RuntimeError",
                message="alpha failed",
            ),
            ErrorReport(
                created_at=now - timedelta(minutes=4),
                endpoint="api.beta",
                status_code=500,
                error_type="OperationalError",
                message="beta failed",
            ),
        ]
    )
    db_session.commit()

    app.config.update(
        {
            "ERROR_REPORT_ALERTS_ENABLED": True,
            "ERROR_REPORT_ALERT_WINDOW_MINUTES": 60,
            "ERROR_REPORT_ALERT_THRESHOLD": 10,
            "ERROR_REPORT_ALERT_TOTAL_THRESHOLD": 2,
            "ERROR_REPORT_ALERT_LIMIT": 10,
            "ERROR_REPORT_ALERT_EMAILS": "",
        }
    )
    ConfigService.clear_cache()

    result = error_report_monitor.send_error_report_alerts()

    assert result["enabled"] is True
    assert result["candidates"] == 0
    assert result["total_count"] == 2
    assert result["sent"] == 0
