from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

from tests.conftest import assert_json_response


def _dt(y, m, d) -> datetime:
    return datetime(y, m, d, 12, 0, 0)


def test_statistics_tc_endpoints(authenticated_client, db_session, sample_matter, sample_user):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    uid = getattr(sample_user, "_test_id", None) or sample_user.id

    wf_tc = Workflow(
        case_id=mid,
        name="TC task",
        status="Completed",
        assignee_id=uid,
        work_hours=2.5,
        created_at=_dt(2026, 1, 5),
        completed_date=date(2026, 1, 6),
        due_date=date(2026, 1, 10),
    )
    wf_missing = Workflow(
        case_id=mid,
        name="Missing TC task",
        status="Completed",
        assignee_id=uid,
        work_hours=None,
        created_at=_dt(2026, 1, 7),
        completed_date=date(2026, 1, 8),
        due_date=date(2026, 1, 12),
    )
    wf_out_of_range = Workflow(
        case_id=mid,
        name="Out of range",
        status="Completed",
        assignee_id=uid,
        work_hours=1.0,
        created_at=_dt(2025, 12, 31),
        completed_date=date(2026, 1, 1),
        due_date=date(2026, 1, 2),
    )
    db_session.add_all([wf_tc, wf_missing, wf_out_of_range])
    db_session.flush()
    wf_tc_id = wf_tc.id
    wf_missing_id = wf_missing.id
    db_session.commit()

    qs = "start=2026-01-01&end=2026-01-31&basis=created"

    # Summary
    resp = authenticated_client.get(f"/statistics/api/tc/summaryNew{qs}")
    data = assert_json_response(resp)
    assert data["total_hours"] == 2.5
    assert data["tc_task_count"] == 1
    assert data["missing_count"] == 1
    assert data["case_count"] == 1
    assert data["assignee_count"] == 1
    assert data["total_task_count"] == 2
    assert data["completed_count"] == 2
    assert data["completed_tc_count"] == 1
    assert data["tc_coverage_rate"] == 50.0
    assert data["hours_per_case"] == 2.5
    assert data["top_assignee_share"] == 100.0

    # Monthly
    resp = authenticated_client.get(f"/statistics/api/tc/monthlyNew{qs}")
    rows = assert_json_response(resp)
    assert rows == [{"month": "2026-01", "total_hours": 2.5, "task_count": 1}]

    # By-case
    resp = authenticated_client.get(f"/statistics/api/tc/by-caseNew{qs}&limit=10")
    rows = assert_json_response(resp)
    assert len(rows) == 1
    assert rows[0]["case_id"] == mid
    assert rows[0]["total_hours"] == 2.5
    assert rows[0]["task_count"] == 1

    # Detail
    resp = authenticated_client.get(f"/statistics/api/tc/detailNew{qs}&limit=10")
    rows = assert_json_response(resp)
    assert len(rows) == 1
    assert rows[0]["workflow_id"] == wf_tc_id
    assert rows[0]["work_hours"] == 2.5
    assert rows[0]["status"] == "Completed"

    # Missing
    resp = authenticated_client.get(f"/statistics/api/tc/missingNew{qs}&limit=10")
    rows = assert_json_response(resp)
    assert len(rows) == 1
    assert rows[0]["workflow_id"] == wf_missing_id

    # Export (TC)
    resp = authenticated_client.get(f"/statistics/api/tc/export.csvNew{qs}&mode=tc&limit=100")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8-sig")
    assert "workflow_id" in text
    assert str(wf_tc_id) in text


def test_statistics_tc_assignee_filter_uses_selected_user_for_mixed_workflow(
    authenticated_client, db_session, sample_matter, sample_user
):
    from app.models.user import User
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    manager_id = getattr(sample_user, "_test_id", None) or sample_user.id

    handler = User(
        username="stats_handler_user",
        email="stats_handler_user@example.com",
        role="patent_staff",
        is_active=True,
    )
    attorney = User(
        username="stats_attorney_user",
        email="stats_attorney_user@example.com",
        role="lead_attorney",
        is_active=True,
    )
    db_session.add_all([handler, attorney])
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="Mixed TC task",
        status="Completed",
        category="WORK",
        assignee_id=handler.id,
        attorney_assignee_id=attorney.id,
        inspector_id=manager_id,
        work_hours=3.0,
        created_at=_dt(2026, 1, 5),
        completed_date=date(2026, 1, 6),
        due_date=date(2026, 1, 10),
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = wf.id
    db_session.commit()

    qs = "start=2026-01-01&end=2026-01-31&basis=created&tc_scope=all" f"&assignee_id={manager_id}"

    resp = authenticated_client.get(f"/statistics/api/tc/summaryNew{qs}")
    summary = assert_json_response(resp)
    assert summary["total_hours"] == 3.0
    assert summary["assignee_count"] == 1
    assert summary["top_assignee_share"] == 100.0

    resp = authenticated_client.get(f"/statistics/api/tc?{qs}")
    rows = assert_json_response(resp)
    assert len(rows) == 1
    assert rows[0]["assignee_id"] == manager_id
    assert rows[0]["total_hours"] == 3.0

    resp = authenticated_client.get(f"/statistics/api/tc/detailNew{qs}&limit=10")
    detail = assert_json_response(resp)
    assert len(detail) == 1
    assert detail[0]["workflow_id"] == wf_id
    assert detail[0]["assignee_id"] == manager_id


def test_workflow_tc_report_view_renders(authenticated_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    wf = Workflow(case_id=mid, name="TC task", status="Completed", created_at=_dt(2026, 1, 5))
    mgmt_wf = Workflow(
        case_id=mid,
        name="MGMT task",
        status="Completed",
        category="MGMT",
        created_at=_dt(2026, 1, 5),
    )
    db_session.add_all([wf, mgmt_wf])
    db_session.commit()

    with patch("app.utils.permissions.can_access_matter", return_value=True):
        resp = authenticated_client.get(f"/workflow/tc/{mid}/view")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "TC task" in html
    assert "MGMT task" not in html
    assert "TC Missing" in html

    with patch("app.utils.permissions.can_access_matter", return_value=True):
        resp = authenticated_client.get(f"/workflow/tc/{mid}/viewNewtc_scope=all")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "MGMT task" in html


def test_statistics_pages_use_local_chart_asset(authenticated_client):
    for path in (
        "/statistics/",
        "/statistics/clients",
        "/statistics/costs",
        "/statistics/tc",
        "/statistics/performance",
    ):
        resp = authenticated_client.get(path)
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "vendor/chart/chart.umd.min.js" in html
        assert "cdn.jsdelivr.net/npm/chart.js" not in html


def test_statistics_pages_bootstrap_ipm_date_before_inline_scripts(authenticated_client):
    ipm_date_bootstrap = "window.AppDate = window.AppDate || {};"

    for path in (
        "/statistics/",
        "/statistics/clients",
        "/statistics/costs",
        "/statistics/tc",
        "/statistics/performance",
    ):
        resp = authenticated_client.get(path)
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert ipm_date_bootstrap in html
        assert "window.AppDate.formatYmd(yearAgo);" in html
        assert html.index(ipm_date_bootstrap) < html.index("window.AppDate.formatYmd(yearAgo);")


def test_statistics_costs_api_handles_decimal_monthly_values(authenticated_client):
    with patch(
        "app.blueprints.statistics.routes.InvoiceService.get_monthly_statistics",
        return_value=[
            {"month": "2026-01", "billed": 3000.0, "paid": 1500.0},
            {"month": "2026-02", "billed": "2200000.0", "paid": "0.0"},
        ],
    ):
        resp = authenticated_client.get("/statistics/api/costsNewstart=2026-01-01&end=2026-02-28")

    rows = assert_json_response(resp)
    assert rows == [
        {
            "month": "2026-01",
            "billed": 3000,
            "paid": 1500,
            "outstanding": 1500,
            "collection_rate": 50.0,
            "running_billed": 3000,
            "running_paid": 1500,
            "running_outstanding": 1500,
        },
        {
            "month": "2026-02",
            "billed": 2200000,
            "paid": 0,
            "outstanding": 2200000,
            "collection_rate": 0.0,
            "running_billed": 2203000,
            "running_paid": 1500,
            "running_outstanding": 2201500,
        },
    ]


def test_workflow_tc_my_bulk_update(authenticated_client, db_session, sample_matter, sample_user):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    uid = getattr(sample_user, "_test_id", None) or sample_user.id

    wf = Workflow(
        case_id=mid,
        name="Missing TC",
        status="Completed",
        assignee_id=uid,
        work_hours=None,
        created_at=_dt(2026, 1, 5),
        completed_date=date(2026, 1, 15),
        due_date=date(2026, 1, 20),
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = wf.id
    db_session.commit()

    resp = authenticated_client.get("/workflow/tc/my")
    assert resp.status_code == 200

    resp = authenticated_client.post(
        "/workflow/tc/my",
        data={
            "workflow_id": [str(wf_id)],
            f"work_hours_{wf_id}": "2.5",
            "basis": "completed",
            "status": "Completed",
            "show": "missing",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    updated = Workflow.query.get(wf_id)
    assert updated is not None
    assert updated.work_hours == 2.5

    resp = authenticated_client.post(
        "/workflow/tc/my",
        data={
            "workflow_id": [str(wf_id)],
            f"work_hours_{wf_id}": "",
            "basis": "completed",
            "status": "Completed",
            "show": "tc",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    updated = Workflow.query.get(wf_id)
    assert updated is not None
    assert updated.work_hours is None


def test_workflow_tc_my_rejects_non_finite_hours(
    authenticated_client, db_session, sample_matter, sample_user
):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    uid = getattr(sample_user, "_test_id", None) or sample_user.id

    wf = Workflow(
        case_id=mid,
        name="Invalid TC",
        status="Completed",
        assignee_id=uid,
        work_hours=1.25,
        created_at=_dt(2026, 1, 5),
        completed_date=date(2026, 1, 15),
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = wf.id
    db_session.commit()

    resp = authenticated_client.post(
        "/workflow/tc/my",
        data={
            "workflow_id": [str(wf_id)],
            f"work_hours_{wf_id}": "nan",
            "basis": "completed",
            "status": "Completed",
            "show": "tc",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    updated = Workflow.query.get(wf_id)
    assert updated is not None
    assert updated.work_hours == 1.25


def test_workflow_tc_my_scope_candidate_excludes_mgmt(
    authenticated_client, db_session, sample_matter, sample_user
):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    uid = getattr(sample_user, "_test_id", None) or sample_user.id

    db_session.add_all(
        [
            Workflow(
                case_id=mid,
                name="TC WORK",
                status="Completed",
                assignee_id=uid,
                category="WORK",
                work_hours=1.0,
                completed_date=date(2026, 1, 10),
                created_at=_dt(2026, 1, 9),
            ),
            Workflow(
                case_id=mid,
                name="TC MGMT",
                status="Completed",
                assignee_id=uid,
                category="MGMT",
                work_hours=2.0,
                completed_date=date(2026, 1, 11),
                created_at=_dt(2026, 1, 9),
            ),
        ]
    )
    db_session.commit()

    resp = authenticated_client.get(
        "/workflow/tc/myNewstart=2026-01-01&end=2026-01-31&basis=completed&status=Completed&show=all"
    )
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "TC WORK" in html
    assert "TC MGMT" not in html

    resp = authenticated_client.get(
        "/workflow/tc/myNewstart=2026-01-01&end=2026-01-31&basis=completed&status=Completed&show=all&tc_scope=all"
    )
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "TC WORK" in html
    assert "TC MGMT" in html


def test_statistics_performance_summary_metrics(
    authenticated_client,
    db_session,
    sample_matter,
    sample_user,
):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    uid = getattr(sample_user, "_test_id", None) or sample_user.id

    rows = [
        Workflow(
            case_id=mid,
            name="Completed with TC",
            status="Completed",
            assignee_id=uid,
            work_hours=2.0,
            created_at=_dt(2026, 1, 2),
            completed_date=date(2026, 1, 5),
        ),
        Workflow(
            case_id=mid,
            name="Completed missing TC",
            status="Completed",
            assignee_id=uid,
            work_hours=None,
            created_at=_dt(2026, 1, 3),
            completed_date=date(2026, 1, 10),
        ),
        Workflow(
            case_id=mid,
            name="Completed unassigned",
            status="Completed",
            assignee_id=None,
            work_hours=1.0,
            created_at=_dt(2026, 1, 4),
            completed_date=date(2026, 1, 12),
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()

    with patch(
        "app.blueprints.statistics.routes.InvoiceService.get_monthly_statistics",
        return_value=[{"month": "2026-01", "billed": "3000.0", "paid": 1500.0}],
    ):
        resp = authenticated_client.get(
            "/statistics/api/performanceNewstart=2026-01-01&end=2026-01-31"
        )
    data = assert_json_response(resp)

    assert data["summary"]["completed_total"] == 3
    assert data["summary"]["completed_with_tc"] == 2
    assert data["summary"]["tc_coverage_rate"] == 66.7
    assert data["summary"]["total_hours"] == 3.0
    assert data["summary"]["completed_per_hour"] == 1.0
    assert data["summary"]["invoice_billed"] == 3000
    assert data["summary"]["invoice_paid"] == 1500
    assert data["summary"]["invoice_collection_rate"] == 50.0
    assert data["summary"]["invoice_per_tc_hour"] == 1000.0

    assert data["monthly"] == [
        {
            "month": "2026-01",
            "count": 3,
            "total_hours": 3.0,
            "avg_hours": 1.5,
            "tc_coverage_rate": 66.7,
        }
    ]

    by_assignee = data["by_assignee"]
    assert len(by_assignee) == 2
    first = by_assignee[0]
    assert first["assignee_id"] == uid
    assert first["completed_count"] == 2
    assert first["tc_input_count"] == 1
    assert first["avg_hours"] == 2.0
    assert first["tc_coverage_rate"] == 50.0


def test_statistics_clients_api_sort_and_search(authenticated_client):
    fake_rows = [
        {"client_id": "1", "client_name": "Alpha Corp", "total_billed": 1000, "total_paid": 900},
        {"client_id": "2", "client_name": "Beta Corp", "total_billed": 1500, "total_paid": 300},
        {"client_id": "3", "client_name": "Gamma LLC", "total_billed": 1200, "total_paid": 1200},
    ]
    with patch(
        "app.blueprints.statistics.routes.InvoiceService.get_client_statistics",
        return_value=fake_rows,
    ):
        resp = authenticated_client.get(
            "/statistics/api/clients"
            "Newstart=2026-01-01&end=2026-01-31&client_q=corp&sort=outstanding&limit=5"
        )

    rows = assert_json_response(resp)
    assert len(rows) == 2
    assert rows[0]["client_name"] == "Beta Corp"
    assert rows[0]["outstanding"] == 1200
    assert rows[0]["collection_rate"] == 20.0


def test_statistics_tc_scope_candidate_excludes_mgmt(
    authenticated_client, db_session, sample_matter, sample_user
):
    from app.models.workflow import Workflow

    mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    uid = getattr(sample_user, "_test_id", None) or sample_user.id

    db_session.add_all(
        [
            Workflow(
                case_id=mid,
                name="WORK task",
                status="Completed",
                assignee_id=uid,
                category="WORK",
                work_hours=1.5,
                created_at=_dt(2026, 1, 10),
                completed_date=date(2026, 1, 11),
            ),
            Workflow(
                case_id=mid,
                name="MGMT task",
                status="Completed",
                assignee_id=uid,
                category="MGMT",
                work_hours=3.0,
                created_at=_dt(2026, 1, 12),
                completed_date=date(2026, 1, 13),
            ),
        ]
    )
    db_session.commit()

    # default scope=candidate should exclude MGMT category
    resp = authenticated_client.get("/statistics/api/tc/summaryNewstart=2026-01-01&end=2026-01-31")
    data = assert_json_response(resp)
    assert data["tc_scope"] == "candidate"
    assert data["total_hours"] == 1.5
    assert data["tc_task_count"] == 1

    # all scope includes both
    resp = authenticated_client.get(
        "/statistics/api/tc/summaryNewstart=2026-01-01&end=2026-01-31&tc_scope=all"
    )
    data = assert_json_response(resp)
    assert data["tc_scope"] == "all"
    assert data["total_hours"] == 4.5
    assert data["tc_task_count"] == 2
