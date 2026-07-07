from datetime import datetime, timedelta
from unittest.mock import patch


def test_notification_log_upserts(db_session):
    from app.extensions import db
    from app.models.notification import NotificationLog
    from app.services.deadlines import deadline_notifications

    # Ensure the model is registered before table creation.
    db.create_all()

    log = NotificationLog(
        entity_type="docket_item",
        entity_id="D1",
        channel="email",
        days_before=7,
        recipient="a@example.com",
        status="failed",
        error_message="boom",
        sent_at=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add(log)
    db_session.commit()
    old_sent_at = log.sent_at

    deadline_notifications._log_notification(
        entity_type="docket_item",
        entity_id="D1",
        channel="email",
        days_before=7,
        recipient="b@example.com",
        status="sent",
        error_message=None,
    )

    updated = NotificationLog.query.filter_by(
        entity_type="docket_item",
        entity_id="D1",
        channel="email",
        days_before=7,
    ).one()
    assert updated.id == log.id
    assert updated.status == "sent"
    assert updated.recipient == "b@example.com"
    assert updated.error_message is None
    assert updated.sent_at > old_sent_at


def test_is_notification_sent_rolls_back_on_query_error():
    from app.services.deadlines import deadline_notifications

    with (
        patch.object(deadline_notifications, "NotificationLog") as mock_notification_log,
        patch("app.services.deadlines.deadline_notifications.db.session.rollback") as mock_rollback,
    ):
        mock_notification_log.query.filter_by.return_value.first.side_effect = RuntimeError("boom")

        sent = deadline_notifications._is_notification_sent(
            entity_type="docket_item",
            entity_id="D1",
            channel="email",
            days_before=7,
        )

        assert sent is False
        mock_rollback.assert_called_once()
