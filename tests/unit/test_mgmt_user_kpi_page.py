from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from app.services.admin.user_kpi_service import build_user_kpi_dashboard


def _login(client, *, user_id: int):
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


def _seed_invoice_for_matter(app, *, matter_id: str, our_ref: str) -> None:
    from app.services.billing.db_core import get_db
    from legacy_billing_schema.db_migrations import init_db

    with app.app_context():
        init_db()
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO business_profile (id, name, currency, next_invoice_no) VALUES (1, 'KPI-BP', 'USD', 1)"
            )
            conn.execute("INSERT INTO clients (id, name) VALUES (1, 'KPI Client')")
            conn.execute(
                """
                INSERT INTO invoices (
                    id, client_id, business_profile_id, number, issue_date, due_date,
                    status, billing_status, payment_status,
                    subtotal, tax, total, currency, vat_rate
                ) VALUES (
                    1, 1, 1, 'KPI-INV-001', '2026-01-10', '2026-01-20',
                    'sent', 'sent', 'pending',
                    1200, 120, 1320, 'USD', 10.0
                )
                """
            )
            conn.execute(
                "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated) VALUES (1, 'Service', 1, 1000, 'service', 0, 1, 0)"
            )
            conn.execute(
                "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated) VALUES (1, 'Admin', 1, 200, 'admin', 0, 0, 0)"
            )
            conn.execute(
                "INSERT INTO invoice_payments (invoice_id, paid_at, amount_minor, currency, verified) VALUES (1, '2026-01-15', 66000, 'USD', 1)"
            )
            conn.execute(
                """
                INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id)
                VALUES (?, ?, 1)
                """,
                (matter_id, our_ref),
            )
            conn.commit()
        finally:
            conn.close()


def test_user_kpi_dashboard_switches_invoice_attribution_basis(
    app, db_session, clean_legacy_invoice_db
):
    from app.models.docket import DocketItem
    from app.models.matter import Matter, MatterStaffAssignment
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog

    manager = User(
        username="kpi_manager",
        email="kpi_manager@example.com",
        display_name="KPI Manager",
        role="mgmt_staff",
        is_active=True,
        staff_party_id="staff-kpi-manager",
    )
    attorney = User(
        username="kpi_attorney",
        email="kpi_attorney@example.com",
        display_name="KPI Attorney",
        role="patent_staff",
        is_active=True,
        staff_party_id="staff-kpi-attorney",
    )
    handler = User(
        username="kpi_handler",
        email="kpi_handler@example.com",
        display_name="KPI Handler",
        role="patent_staff",
        is_active=True,
        staff_party_id="staff-kpi-handler",
    )
    db_session.add_all([manager, attorney, handler])
    db_session.commit()

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="KPI-26-0001",
        right_name="KPI Text Text",
        created_at=datetime.utcnow(),
    )
    db_session.add(matter)
    db_session.flush()
    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=str(matter.matter_id),
                staff_party_id=manager.staff_party_id,
                staff_role_code="manager",
                seq=1,
            ),
            MatterStaffAssignment(
                matter_id=str(matter.matter_id),
                staff_party_id=attorney.staff_party_id,
                staff_role_code="attorney",
                seq=1,
            ),
            MatterStaffAssignment(
                matter_id=str(matter.matter_id),
                staff_party_id=handler.staff_party_id,
                staff_role_code="handler",
                seq=1,
            ),
            Workflow(
                case_id=str(matter.matter_id),
                name="OA Text",
                status="Completed",
                assignee_id=handler.id,
                attorney_assignee_id=attorney.id,
                inspector_id=manager.id,
                work_hours=3.5,
                category="WORK",
                completed_date=date(2026, 1, 11),
                created_at=datetime(2026, 1, 9, 9, 0, 0),
            ),
            Workflow(
                case_id=str(matter.matter_id),
                name="Text Text",
                status="Pending",
                assignee_id=handler.id,
                attorney_assignee_id=attorney.id,
                inspector_id=manager.id,
                due_date=date(2026, 1, 20),
                category="WORK",
                created_at=datetime.utcnow(),
            ),
            WorkLog(
                docket_id=uuid.uuid4().hex,
                matter_id=str(matter.matter_id),
                task_name="Text Text",
                task_category="WORK",
                due_date=date(2026, 1, 11),
                status="completed",
                completed_by_id=handler.id,
                completed_at=datetime(2026, 1, 12, 10, 0, 0),
                owner_staff_party_id=handler.staff_party_id,
            ),
            WorkLog(
                docket_id=uuid.uuid4().hex,
                matter_id=str(matter.matter_id),
                task_name="Text Text",
                task_category="MGMT",
                due_date=date(2026, 1, 16),
                status="completed",
                completed_by_id=None,
                completed_at=datetime(2026, 1, 15, 14, 0, 0),
                owner_staff_party_id=handler.staff_party_id,
            ),
            DocketItem(
                docket_id=uuid.uuid4().hex,
                matter_id=str(matter.matter_id),
                category="WORK",
                name_ref="OA_REPLY",
                due_date="2026-01-18",
                owner_staff_party_id=handler.staff_party_id,
                done_date=None,
                is_deleted=False,
            ),
        ]
    )
    db_session.commit()

    _seed_invoice_for_matter(app, matter_id=str(matter.matter_id), our_ref=matter.our_ref)

    manager_dash = build_user_kpi_dashboard(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        owner_basis="manager",
        sort_key="activity",
        today=date(2026, 1, 31),
    )
    attorney_dash = build_user_kpi_dashboard(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        owner_basis="attorney",
        sort_key="activity",
        today=date(2026, 1, 31),
    )
    primary_dash = build_user_kpi_dashboard(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        owner_basis="primary",
        sort_key="activity",
        today=date(2026, 1, 31),
    )

    manager_rows = {row["assignee"]: row for row in manager_dash["rows"]}
    attorney_rows = {row["assignee"]: row for row in attorney_dash["rows"]}
    primary_rows = {row["assignee"]: row for row in primary_dash["rows"]}

    assert manager_rows["KPI Manager"]["invoice_count"] == 1.0
    assert manager_rows["KPI Manager"]["billed_by_currency"]["USD"] == 1000.0
    assert manager_rows["KPI Manager"]["collected_by_currency"]["USD"] == 500.0
    assert manager_rows["KPI Attorney"]["invoice_count"] == 0.0
    assert manager_rows["KPI Handler"]["invoice_count"] == 0.0

    assert attorney_rows["KPI Attorney"]["invoice_count"] == 1.0
    assert attorney_rows["KPI Attorney"]["billed_by_currency"]["USD"] == 1000.0
    assert attorney_rows["KPI Manager"]["invoice_count"] == 0.0
    assert attorney_rows["KPI Handler"]["invoice_count"] == 0.0

    assert primary_rows["KPI Handler"]["invoice_count"] == 1.0
    assert primary_rows["KPI Handler"]["activity_total"] == 3
    assert primary_rows["KPI Handler"]["worklog_completed"] == 2
    assert primary_rows["KPI Handler"]["worklog_active_days"] == 2
    assert primary_rows["KPI Handler"]["worklog_due_tracked_completed"] == 2
    assert primary_rows["KPI Handler"]["worklog_on_time_rate"] == 50.0
    assert primary_rows["KPI Handler"]["worklog_avg_delay_days"] == 1.0
    assert primary_rows["KPI Handler"]["worklog_mix_display"] == "WORK 1 / MGMT 1"
    assert primary_rows["KPI Handler"]["worklog_focus_label"] == ""
    assert primary_rows["KPI Handler"]["days_since_last_activity"] == 16
    assert primary_rows["KPI Handler"]["backlog_total"] == 2
    assert primary_rows["KPI Handler"]["overdue_total"] == 2
    assert primary_dash["summary"]["invoice_capture_rate"] == 100.0
    assert primary_dash["summary"]["worklog_on_time_rate"] == 50.0
    assert primary_dash["summary"]["billed_display"] == "USD 1,000"


def test_mgmt_kpi_page_renders_management_board(app, client, db_session, clean_legacy_invoice_db):
    from app.models.matter import Matter, MatterStaffAssignment
    from app.models.user import User

    mgmt_user = User(
        username="mgmt_kpi_viewer",
        email="mgmt_kpi_viewer@example.com",
        display_name="KPI Viewer",
        role="mgmt_staff",
        is_active=True,
        staff_party_id="staff-kpi-viewer",
    )
    attorney = User(
        username="kpi_view_attorney",
        email="kpi_view_attorney@example.com",
        display_name="KPI Page Attorney",
        role="patent_staff",
        is_active=True,
        staff_party_id="staff-kpi-page-attorney",
    )
    db_session.add_all([mgmt_user, attorney])
    db_session.commit()

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="KPI-PAGE-0001",
        right_name="Text KPI Text",
        created_at=datetime.utcnow(),
    )
    db_session.add(matter)
    db_session.flush()
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(matter.matter_id),
            staff_party_id=attorney.staff_party_id,
            staff_role_code="attorney",
            seq=1,
        )
    )
    db_session.commit()

    _seed_invoice_for_matter(app, matter_id=str(matter.matter_id), our_ref=matter.our_ref)

    mgmt_client = _login(client, user_id=mgmt_user.id)
    res = mgmt_client.get("/mgmt/kpiNewstart=2026-01-01&end=2026-01-31&owner_basis=attorney")
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert "Operations KPI Board" in html
    assert "Revenue Attributed: Responsible attorney" in html
    assert "KPI Page Attorney" in html
    assert "USD 1,000" in html
