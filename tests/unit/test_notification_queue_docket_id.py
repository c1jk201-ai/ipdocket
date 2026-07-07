from datetime import date


def test_docket_reminders_store_text_docket_id(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.models.notification_queue import NotificationQueue
    from app.services.productivity import reminder_service

    docket_id = "8614358de5d844f594eabe29c6b2ef4a"
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id="matter_text_docket",
            category="NOTICE",
            name_ref="NOTICE:OA:text-docket",
            due_date="2026-01-08",
        )
    )
    db_session.commit()

    monkeypatch.setattr(reminder_service, "get_today", lambda: date(2026, 1, 1))
    monkeypatch.setattr(reminder_service, "get_user_id", lambda: "user-1")

    reminder_service.ensure_docket_reminders(matter_id="matter_text_docket", horizon_days=14)

    rows = NotificationQueue.query.order_by(NotificationQueue.remind_on.asc()).all()
    assert len(rows) == 3
    assert {row.docket_id for row in rows} == {docket_id}
