from __future__ import annotations

from datetime import date

from app.models.case_flat_index import CaseFlatIndex
from app.models.docket import DocketItem
from app.models.legacy_finance import ExternalInvoiceCaseLink, ExternalInvoiceCaseMap
from app.models.workflow import Workflow


def _seed_invoice(
    app,
    *,
    invoice_id: int,
    number: str,
    status: str = "sent",
    billing_status: str = "sent",
    payment_status: str = "unpaid",
    payment_verified: int = 0,
) -> None:
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (1, 'Client A')")
        conn.execute(
            """
            INSERT INTO invoices (
                id,
                client_id,
                business_profile_id,
                number,
                issue_date,
                due_date,
                status,
                billing_status,
                payment_status,
                payment_verified,
                currency,
                subtotal,
                tax,
                total,
                subtotal_minor,
                tax_minor,
                total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_id,
                1,
                1,
                number,
                "2026-02-13",
                "2026-03-13",
                status,
                billing_status,
                payment_status,
                payment_verified,
                "USD",
                1000,
                100,
                1100,
                1000,
                100,
                1100,
            ),
        )
        conn.commit()
        conn.close()


def test_maybe_notify_manager_followup_for_invoice_sends_once(
    app, db_session, sample_user, sample_matter, clean_legacy_invoice_db
):
    from app.blueprints.billing_invoices.db import get_db
    from app.services.billing.invoice_manager_followup_service import (
        maybe_notify_manager_followup_for_invoice,
    )

    user = db_session.merge(sample_user)
    matter = db_session.merge(sample_matter)
    matter_id = str(getattr(matter, "_test_matter_id", None) or matter.matter_id)
    invoice_id = 454

    _seed_invoice(
        app,
        invoice_id=invoice_id,
        number="INV-0454",
        status="paid",
        billing_status="sent",
        payment_status="paid",
        payment_verified=1,
    )

    docket = DocketItem(
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
        name_free="RegistrationDeadline",
        due_date="2026-04-09",
    )
    db_session.add(docket)
    db_session.flush()

    workflow = Workflow(
        case_id=matter_id,
        name="RegistrationDeadline",
        status="Pending",
        business_code=f"DOCKET:{docket.docket_id}",
        category="MGMT",
        due_date=date(2026, 4, 9),
        inspector_id=user.id,
        created_by_id=user.id,
    )
    db_session.add(workflow)
    db_session.add(CaseFlatIndex(matter_id=matter_id, manager_id=str(user.id)))
    db_session.add(
        ExternalInvoiceCaseMap(
            matter_id=matter_id,
            external_invoice_id=invoice_id,
            is_deleted=False,
        )
    )
    db_session.commit()


    result1 = maybe_notify_manager_followup_for_invoice(
        action="invoice.payment.verify",
        invoice_id=invoice_id,
        meta={"ok": True},
        actor_id=user.id,
    )
    result2 = maybe_notify_manager_followup_for_invoice(
        action="invoice.payment.verify",
        invoice_id=invoice_id,
        meta={"ok": True},
        actor_id=user.id,
    )

    assert result1["status"] == "ok"
    assert result1["sent"] == 1
    assert result1["workflow_ids"] == [workflow.id]
    assert result2["status"] == "ok"
    assert result2["sent"] == 0

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            """
            SELECT action, meta
            FROM audit_log
            WHERE action='invoice.manager_followup_notice' AND target_type='invoice' AND target_id=?
            """,
            (invoice_id,),
        ).fetchone()
        conn.close()

    assert row is not None
    assert row["action"] == "invoice.manager_followup_notice"
    assert f'"matter_id":"{matter_id}"' in (row["meta"] or "")


def test_maybe_notify_manager_followup_for_invoice_accepts_external_invoice_case_link(
    app, db_session, sample_user, sample_matter, clean_legacy_invoice_db
):
    from app.services.billing.invoice_manager_followup_service import (
        maybe_notify_manager_followup_for_invoice,
    )

    user = db_session.merge(sample_user)
    matter = db_session.merge(sample_matter)
    matter_id = str(getattr(matter, "_test_matter_id", None) or matter.matter_id)
    invoice_id = 455

    _seed_invoice(
        app,
        invoice_id=invoice_id,
        number="INV-0455",
        status="tax_issued",
        billing_status="tax_issued",
        payment_status="unpaid",
        payment_verified=0,
    )

    docket = DocketItem(
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
        name_free="RegistrationDeadline",
        due_date="2026-04-09",
    )
    db_session.add(docket)
    db_session.flush()

    workflow = Workflow(
        case_id=matter_id,
        name="RegistrationDeadline",
        status="Pending",
        business_code=f"DOCKET:{docket.docket_id}",
        category="MGMT",
        due_date=date(2026, 4, 9),
        inspector_id=user.id,
        created_by_id=user.id,
    )
    db_session.add(workflow)
    db_session.add(CaseFlatIndex(matter_id=matter_id, manager_id=str(user.id)))
    db_session.add(
        ExternalInvoiceCaseLink(
            matter_id=matter_id,
            external_invoice_id=invoice_id,
            external_invoice_number="INV-0455",
            is_deleted=False,
        )
    )
    db_session.commit()


    result = maybe_notify_manager_followup_for_invoice(
        action="invoice.tax_issued",
        invoice_id=invoice_id,
        actor_id=user.id,
    )

    assert result["status"] == "ok"
    assert result["sent"] == 1
    assert result["workflow_ids"] == [workflow.id]
