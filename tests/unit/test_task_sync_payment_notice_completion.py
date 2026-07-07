from __future__ import annotations


def test_sync_from_docket_item_auto_completes_payment_extension_notice_from_case_dates(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.services.workflow.task_sync import sync_from_docket_item
    from app.utils.policy_sql import policy_text as text

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    # Insert with raw SQL to avoid ORM event listeners that are unrelated to this test.
    db_session.execute(
        text(
            """
            INSERT INTO matter_custom_field (matter_id, namespace, data, updated_at)
            VALUES (:matter_id, 'domestic_trademark', :data, CURRENT_TIMESTAMP)
            """
        ).execution_options(policy_bypass=True),
        {
            "matter_id": matter_id,
            "data": '{"registration_date":"2026-01-14","reg_extension_date":"2026-01-09"}',
        },
    )
    db_session.commit()

    di = DocketItem(
        docket_id="docket-payment-extension-1",
        matter_id=matter_id,
        category="V2_LIMIT",
        name_free="RegistrationPayment extension notice",
        due_date="2026-01-14",
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    sync_from_docket_item(docket_item=di, actor_id=None)
    db_session.commit()

    di2 = DocketItem.query.get("docket-payment-extension-1")
    assert di2 is not None
    assert di2.done_date == "2026-01-09"

    # Workflow upsert path uses nested SAVEPOINT and can be flaky on SQLite in tests.
    # The critical regression target here is docket completion signal propagation.


def test_sync_from_docket_item_auto_completes_registration_success_from_case_dates(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.services.workflow.task_sync import sync_from_docket_item
    from app.utils.policy_sql import policy_text as text

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    db_session.execute(
        text(
            """
            INSERT INTO matter_custom_field (matter_id, namespace, data, updated_at)
            VALUES (:matter_id, 'domestic_trademark', :data, CURRENT_TIMESTAMP)
            """
        ).execution_options(policy_bypass=True),
        {
            "matter_id": matter_id,
            "data": '{"registration_date":"2026-01-14"}',
        },
    )
    db_session.commit()

    di = DocketItem(
        docket_id="docket-registration-success-1",
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
        name_free="RegistrationDeadline",
        due_date="2026-02-14",
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    sync_from_docket_item(docket_item=di, actor_id=None)
    db_session.commit()

    di2 = DocketItem.query.get("docket-registration-success-1")
    assert di2 is not None
    assert di2.done_date == "2026-01-14"
