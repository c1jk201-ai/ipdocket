from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import text


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def _prepare_owner_user(db_session, sample_user):
    user = db_session.merge(sample_user)
    user.is_active = True
    user.email = user.email or "owner@example.com"
    user.staff_party_id = user.staff_party_id or "staff-owner-1"
    db_session.add(user)
    db_session.commit()
    return user


def _disable_annuity_management_for_matter(db_session, matter_id: str):
    from app.models.client import Client
    from app.models.ip_records import MatterCustomField

    client = Client(
        name=f"Text-{uuid.uuid4().hex[:8]}",
        extra={"annuity_management_disabled": True},
    )
    db_session.add(client)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(matter_id),
            namespace="domestic_patent",
            data={"client_id": str(client.id), "client_name": client.name},
        )
    )
    db_session.commit()
    return client


def test_get_upcoming_docket_items_uses_system_config_days(db_session, sample_matter, sample_user):
    from app.models.docket import DocketItem
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    target_days = 5
    due_date = (date.today() + timedelta(days=target_days)).isoformat()
    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="CONFIG DAY TEST",
        due_date=due_date,
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(docket)
    SystemConfig.set_config("DEADLINE_REMINDER_DAYS", str(target_days))
    db_session.commit()
    ConfigService.clear_cache()

    rows = deadline_notifications.get_upcoming_docket_items()
    assert any(item.docket_id == docket.docket_id and days == target_days for item, days in rows)


def test_get_upcoming_annuity_items_excludes_annuity_management_disabled_matters(
    db_session, sample_matter
):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    target_days = 14
    _disable_annuity_management_for_matter(db_session, _matter_id(sample_matter))

    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=(date.today() + timedelta(days=target_days)).isoformat(),
        annuity_status="pending",
    )
    db_session.add(annuity)
    db_session.commit()

    rows = deadline_notifications.get_upcoming_annuity_items(days_before_list=[target_days])
    assert all(item.annuity_id != annuity.annuity_id for item, _ in rows)


def test_docket_notification_dedupe_includes_due_date(
    db_session, sample_matter, sample_user, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.notification import NotificationLog
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    due_1 = (date.today() + timedelta(days=7)).isoformat()
    due_2 = (date.today() + timedelta(days=10)).isoformat()

    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="DUE DATE DEDUPE TEST",
        due_date=due_1,
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(docket)
    db_session.commit()

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", lambda self, payload: True)

    assert (
        deadline_notifications.send_docket_item_notification(
            docket, 7, deadline_notifications.EmailChannel()
        )
        is True
    )
    db_session.expire_all()

    first_logs = (
        NotificationLog.query.filter_by(
            entity_type="docket_item",
            entity_id=docket.docket_id,
            channel="email",
            days_before=7,
        )
        .order_by(NotificationLog.id.asc())
        .all()
    )
    assert len(first_logs) == 1
    assert first_logs[0].due_date and first_logs[0].due_date.isoformat() == due_1

    docket.due_date = due_2
    db_session.add(docket)
    db_session.commit()

    assert (
        deadline_notifications.send_docket_item_notification(
            docket, 7, deadline_notifications.EmailChannel()
        )
        is True
    )
    db_session.expire_all()

    all_logs = (
        NotificationLog.query.filter_by(
            entity_type="docket_item",
            entity_id=docket.docket_id,
            channel="email",
            days_before=7,
        )
        .order_by(NotificationLog.id.asc())
        .all()
    )
    assert len(all_logs) == 2
    assert {log.due_date.isoformat() for log in all_logs if log.due_date} == {due_1, due_2}


def test_retry_failed_deadline_notifications_updates_failed_to_sent(
    app, db_session, sample_matter, sample_user, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.notification import NotificationLog
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    app.config["MAIL_DEFAULT_SENDER"] = "noreply@example.com"
    due_date = date.today() + timedelta(days=7)

    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="RETRY TEST",
        due_date=due_date.isoformat(),
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(docket)
    db_session.commit()

    db_session.add(
        NotificationLog(
            entity_type="docket_item",
            entity_id=docket.docket_id,
            channel="email",
            days_before=7,
            due_date=due_date,
            recipient=user.email,
            status="failed",
            error_message="smtp down",
            sent_at=datetime.utcnow() - timedelta(minutes=30),
        )
    )
    SystemConfig.set_config("DEADLINE_NOTIFICATION_ENABLED", "true")
    db_session.commit()
    ConfigService.clear_cache()

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", lambda self, payload: True)

    sent, failed = deadline_notifications.retry_failed_deadline_notifications(
        channel=deadline_notifications.EmailChannel(),
        lookback_days=1,
        batch_size=50,
    )

    assert sent == 1
    assert failed == 0
    updated = NotificationLog.query.filter_by(
        entity_type="docket_item",
        entity_id=docket.docket_id,
        channel="email",
        days_before=7,
        due_date=due_date,
    ).one()
    assert updated.status == "sent"
    assert updated.error_message is None


def test_deadline_email_enabled_precedence_over_legacy_key(app, db_session):
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import deadline_notifications

    app.config["MAIL_DEFAULT_SENDER"] = "noreply@example.com"
    SystemConfig.set_config("DEADLINE_NOTIFICATION_ENABLED", "false")
    SystemConfig.set_config("DEADLINE_EMAIL_ENABLED", "true")
    db_session.commit()
    ConfigService.clear_cache()

    assert deadline_notifications.is_channel_enabled("email") is True


def test_deadline_email_disabled_skips_email_channel_without_failure_logs(
    app, db_session, sample_matter, sample_user, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.notification import NotificationLog
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    app.config["MAIL_DEFAULT_SENDER"] = "noreply@example.com"
    due = (date.today() + timedelta(days=7)).isoformat()
    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="EMAIL DISABLED TEST",
        due_date=due,
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(docket)
    db_session.commit()

    SystemConfig.set_config("DEADLINE_NOTIFICATION_ENABLED", "true")
    SystemConfig.set_config("DEADLINE_EMAIL_ENABLED", "false")
    db_session.commit()
    ConfigService.clear_cache()

    send_called = {"count": 0}

    def _send_stub(self, payload):
        send_called["count"] += 1
        return False

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", _send_stub)
    sent, failed = deadline_notifications.send_all_deadline_notifications(
        channel=deadline_notifications.EmailChannel(),
        days_before_list=[7],
        log=True,
    )

    assert sent == 0
    assert failed == 0
    assert send_called["count"] == 0
    assert NotificationLog.query.filter_by(channel="email").count() == 0


def test_alarm_section_uses_total_count_not_page_length(db_session, sample_matter):
    from app.blueprints.case.services.detail_context import _build_alarm_section
    from app.models.docket import DocketItem
    from app.models.notification import NotificationLog

    mid = _matter_id(sample_matter)
    logged_due = date.today() + timedelta(days=30)

    for i in range(205):
        docket = DocketItem(
            matter_id=mid,
            category="REMINDER",
            name_ref=f"ALARM-{i}",
            due_date=(date.today() + timedelta(days=7)).isoformat(),
        )
        db_session.add(docket)
        db_session.flush()
        db_session.add(
            NotificationLog(
                entity_type="docket_item",
                entity_id=docket.docket_id,
                channel="email",
                days_before=7,
                due_date=logged_due,
                recipient="owner@example.com",
                status="sent",
            )
        )
    db_session.commit()

    data = _build_alarm_section({"_mid_str": mid})

    assert len(data["alarm_deadline_logs"]) == 200
    assert data["alarm_deadline_total_count"] == 205
    assert data["alarm_total_count"] == 205
    # Logged due-date snapshot should be shown, not today's live docket due-date.
    assert all(row.get("due_date") == logged_due for row in data["alarm_deadline_logs"])


def test_alarm_section_uses_trademark_renewal_label_for_annuity_logs(
    db_session, sample_matter
):
    from app.blueprints.case.services.detail_context import _build_alarm_section
    from app.models.matter_facts import MatterFacts
    from app.models.ip_records import AnnuityItem
    from app.models.notification import NotificationLog

    mid = _matter_id(sample_matter)
    matter = db_session.merge(sample_matter)
    matter.our_ref = "26TD0001US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    db_session.add(matter)
    db_session.add(MatterFacts(matter_id=mid, right_type_norm="TRADEMARK"))
    annuity = AnnuityItem(
        matter_id=mid,
        cycle_no=10,
        due_date=(date.today() + timedelta(days=30)).isoformat(),
        annuity_status="pending",
    )
    db_session.add(annuity)
    db_session.flush()
    db_session.add(
        NotificationLog(
            entity_type="annuity_item",
            entity_id=annuity.annuity_id,
            channel="email",
            days_before=30,
            due_date=date.today() + timedelta(days=30),
            recipient="owner@example.com",
            status="sent",
        )
    )
    db_session.commit()

    data = _build_alarm_section({"_mid_str": mid})

    assert data["alarm_deadline_logs"][0]["title"] == "Section 8/9 Renewal"


def test_get_upcoming_docket_items_includes_extended_due_only_rows(
    db_session, sample_matter, sample_user
):
    from app.models.docket import DocketItem
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    target_days = 3
    ext_due = (date.today() + timedelta(days=target_days)).isoformat()
    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="EXT ONLY TEST",
        due_date=None,
        extended_due_date=ext_due,
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(docket)
    db_session.commit()

    rows = deadline_notifications.get_upcoming_docket_items(days_before_list=[target_days])
    assert any(item.docket_id == docket.docket_id and days == target_days for item, days in rows)


def test_send_docket_notification_unassigned_uses_fallback_email(
    db_session, sample_matter, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import deadline_notifications

    due = (date.today() + timedelta(days=7)).isoformat()
    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="UNASSIGNED FALLBACK TEST",
        due_date=due,
        owner_staff_party_id=None,
    )
    db_session.add(docket)
    SystemConfig.set_config("DEADLINE_UNASSIGNED_FALLBACK_EMAIL", "alerts@example.com")
    db_session.commit()
    ConfigService.clear_cache()

    captured = {}

    def _send_stub(self, payload):
        captured["recipient_email"] = payload.recipient_email
        captured["recipient_name"] = payload.recipient_name
        return True

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", _send_stub)

    sent = deadline_notifications.send_docket_item_notification(
        docket,
        7,
        deadline_notifications.EmailChannel(),
    )
    assert sent is True
    assert captured["recipient_email"] == "alerts@example.com"
    assert captured["recipient_name"] == "Unassigned (Fallback)"


def test_get_upcoming_docket_items_excludes_soft_deleted_rows(
    db_session, sample_matter, sample_user
):
    from app.models.docket import DocketItem
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    target_days = 4
    due = (date.today() + timedelta(days=target_days)).isoformat()
    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="SOFT-DELETED-DOCKET",
        due_date=due,
        owner_staff_party_id=user.staff_party_id,
        is_deleted=True,
    )
    db_session.add(docket)
    db_session.commit()

    rows = deadline_notifications.get_upcoming_docket_items(days_before_list=[target_days])
    assert all(item.docket_id != docket.docket_id for item, _ in rows)


def test_send_docket_notification_skips_soft_deleted_row(
    db_session, sample_matter, sample_user, monkeypatch
):
    from app.models.docket import DocketItem
    from app.services.deadlines import deadline_notifications

    user = _prepare_owner_user(db_session, sample_user)
    due = (date.today() + timedelta(days=7)).isoformat()
    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="REMINDER",
        name_ref="SOFT-DELETED-SEND",
        due_date=due,
        owner_staff_party_id=user.staff_party_id,
        is_deleted=True,
    )
    db_session.add(docket)
    db_session.commit()

    called = {"count": 0}

    def _send_stub(self, payload):
        called["count"] += 1
        return True

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", _send_stub)
    sent = deadline_notifications.send_docket_item_notification(
        docket,
        7,
        deadline_notifications.EmailChannel(),
    )
    assert sent is False
    assert called["count"] == 0


def test_get_upcoming_annuity_items_excludes_soft_deleted_rows(db_session, sample_matter):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    target_days = 6
    due = (date.today() + timedelta(days=target_days)).isoformat()
    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=due,
        annuity_status="pending",
        is_deleted=True,
    )
    db_session.add(annuity)
    db_session.commit()

    rows = deadline_notifications.get_upcoming_annuity_items(days_before_list=[target_days])
    assert all(item.annuity_id != annuity.annuity_id for item, _ in rows)


def test_get_upcoming_annuity_items_uses_legal_due_basis_by_default(
    db_session, sample_matter, monkeypatch
):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    monkeypatch.setattr(
        deadline_notifications,
        "_annuity_reminder_due_basis",
        lambda: deadline_notifications.ANNUITY_REMINDER_DUE_BASIS_LEGAL,
    )

    internal_days = 5
    legal_days = 8
    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=(date.today() + timedelta(days=legal_days)).isoformat(),
        internal_due_date=(date.today() + timedelta(days=internal_days)).isoformat(),
        annuity_status="pending",
    )
    db_session.add(annuity)
    db_session.commit()

    rows_internal = deadline_notifications.get_upcoming_annuity_items(
        days_before_list=[internal_days]
    )
    rows_legal = deadline_notifications.get_upcoming_annuity_items(days_before_list=[legal_days])

    assert all(item.annuity_id != annuity.annuity_id for item, _ in rows_internal)
    assert any(item.annuity_id == annuity.annuity_id for item, _ in rows_legal)


def test_get_upcoming_annuity_items_can_use_effective_due_basis(
    db_session, sample_matter, monkeypatch
):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    monkeypatch.setattr(
        deadline_notifications,
        "_annuity_reminder_due_basis",
        lambda: deadline_notifications.ANNUITY_REMINDER_DUE_BASIS_EFFECTIVE,
    )

    internal_days = 4
    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=(date.today() + timedelta(days=9)).isoformat(),
        internal_due_date=(date.today() + timedelta(days=internal_days)).isoformat(),
        annuity_status="pending",
    )
    db_session.add(annuity)
    db_session.commit()

    rows = deadline_notifications.get_upcoming_annuity_items(days_before_list=[internal_days])
    assert any(
        item.annuity_id == annuity.annuity_id for item, days in rows if days == internal_days
    )


def test_get_upcoming_annuity_items_normalizes_legacy_due_date_strings(db_session, sample_matter):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    target_days = 7
    due = date.today() + timedelta(days=target_days)
    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=due.isoformat(),
        annuity_status="pending",
    )
    db_session.add(annuity)
    db_session.commit()

    db_session.execute(
        text("UPDATE annuity_item SET due_date = :due WHERE annuity_id = :annuity_id"),
        {"due": due.strftime("%Y/%m/%d"), "annuity_id": annuity.annuity_id},
    )
    db_session.commit()

    rows = deadline_notifications.get_upcoming_annuity_items(days_before_list=[target_days])
    assert any(item.annuity_id == annuity.annuity_id for item, _ in rows)


def test_get_upcoming_annuity_items_skips_domestic_prepaid_cycles(db_session, sample_matter):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    matter = db_session.merge(sample_matter)
    matter.right_group = "DOM"
    db_session.add(matter)
    db_session.flush()

    target_days = 7
    due = (date.today() + timedelta(days=target_days)).isoformat()
    annuity_prepaid = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=2,
        due_date=due,
        annuity_status="pending",
    )
    annuity_open = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=due,
        annuity_status="pending",
    )
    db_session.add_all([annuity_prepaid, annuity_open])
    db_session.commit()

    rows = deadline_notifications.get_upcoming_annuity_items(days_before_list=[target_days])
    matched_ids = {item.annuity_id for item, _ in rows}
    assert annuity_prepaid.annuity_id not in matched_ids
    assert annuity_open.annuity_id in matched_ids


def test_send_annuity_notification_logs_missing_recipient_as_failed(
    db_session, sample_matter, monkeypatch
):
    from app.models.notification import NotificationLog
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=4,
        due_date=(date.today() + timedelta(days=7)).isoformat(),
        annuity_status="pending",
        owner_staff_party_id=None,
    )
    db_session.add(annuity)
    db_session.commit()

    called = {"count": 0}

    def _send_stub(self, payload):
        called["count"] += 1
        return True

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", _send_stub)
    sent = deadline_notifications.send_annuity_item_notification(
        annuity,
        7,
        deadline_notifications.EmailChannel(),
    )

    assert sent is False
    assert called["count"] == 0
    log = NotificationLog.query.filter_by(
        entity_type="annuity_item",
        entity_id=annuity.annuity_id,
        channel="email",
        days_before=7,
    ).first()
    assert log is not None
    assert log.status == "failed"
    assert log.error_message == "missing_recipient"


def test_send_annuity_notification_uses_trademark_renewal_title(
    db_session, sample_matter, sample_user, monkeypatch
):
    from app.models.matter_facts import MatterFacts
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    matter = db_session.merge(sample_matter)
    matter.our_ref = "26TD0001US"
    db_session.add(matter)
    user = _prepare_owner_user(db_session, sample_user)
    db_session.add(MatterFacts(matter_id=_matter_id(sample_matter), right_type_norm="TRADEMARK"))
    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=10,
        due_date=(date.today() + timedelta(days=7)).isoformat(),
        annuity_status="pending",
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(annuity)
    db_session.commit()

    sent_payloads = []

    def _send_stub(self, payload):
        sent_payloads.append(payload)
        return True

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", _send_stub)
    sent = deadline_notifications.send_annuity_item_notification(
        annuity,
        7,
        deadline_notifications.EmailChannel(),
    )

    assert sent is True
    assert sent_payloads[0].title == "Trademark Section 8/9 Renewal"


def test_send_annuity_notification_skips_domestic_prepaid_cycle(
    db_session, sample_matter, sample_user, monkeypatch
):
    from app.models.ip_records import AnnuityItem
    from app.services.deadlines import deadline_notifications

    matter = db_session.merge(sample_matter)
    matter.right_group = "DOM"
    db_session.add(matter)

    user = _prepare_owner_user(db_session, sample_user)
    annuity = AnnuityItem(
        matter_id=_matter_id(sample_matter),
        cycle_no=3,
        due_date=(date.today() + timedelta(days=7)).isoformat(),
        annuity_status="pending",
        owner_staff_party_id=user.staff_party_id,
    )
    db_session.add(annuity)
    db_session.commit()

    called = {"count": 0}

    def _send_stub(self, payload):
        called["count"] += 1
        return True

    monkeypatch.setattr(deadline_notifications.EmailChannel, "send", _send_stub)
    sent = deadline_notifications.send_annuity_item_notification(
        annuity,
        7,
        deadline_notifications.EmailChannel(),
    )
    assert sent is False
    assert called["count"] == 0
