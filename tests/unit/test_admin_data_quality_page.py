from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta

from app.models.case import Case
from app.models.deadline import Deadline
from app.models.docket import DocketItem
from app.models.matter import Matter
from app.models.parse_failure import ParseFailure


def _extract_issue_count(html: str, label: str) -> int:
    match = re.search(
        rf"<td class=\"fw-semibold\">{re.escape(label)}</td>\s*<td class=\"text-end\">(\d+)</td>",
        html,
    )
    assert match is not None
    return int(match.group(1))


def test_admin_data_quality_page_shows_detected_issues(admin_client, db_session, monkeypatch):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()

    matter_missing_name = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"DQ-{uuid.uuid4().hex[:8]}",
        right_name=None,
        matter_type="PATENT",
        created_at=datetime.utcnow(),
    )
    orphan_docket = DocketItem(
        docket_id=uuid.uuid4().hex,
        matter_id="MISSING-MATTER-1",
        category="LEGAL",
        name_ref="OA Text",
        due_date="2026-03-01",
        done_date=None,
        owner_staff_party_id=None,
        is_deleted=False,
    )
    parse_failure = ParseFailure(
        kind="int",
        source="test_admin_data_quality_page",
        field_name="cycle_no",
        raw_value="abc",
        error="ValueError",
        created_at=datetime.utcnow() - timedelta(days=1),
    )

    db_session.add_all([matter_missing_name, orphan_docket, parse_failure])
    db_session.commit()

    res = admin_client.get(
        "/admin/data-quality?sample=20&parse_days=30",
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
        },
    )
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert "Matters Missing Title" in html
    assert "Orphan Dockets" in html
    assert "MISSING-MATTER-1" in html
    assert "test_admin_data_quality_page" in html


def test_admin_data_quality_page_separates_legacy_v2_limit_missing_due(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"DQ-{uuid.uuid4().hex[:8]}",
        right_name="Text Text",
        matter_type="PATENT",
        created_at=datetime.utcnow(),
    )
    legacy_v2 = DocketItem(
        docket_id=uuid.uuid4().hex,
        matter_id=str(matter.matter_id),
        category="V2_LIMIT",
        name_ref=None,
        name_free="Text",
        due_date=None,
        done_date=None,
        owner_staff_party_id="staff-v2",
        raw_id="LimitHistory:LeftMenu0001:test",
        is_deleted=False,
    )
    operational_missing_due = DocketItem(
        docket_id=uuid.uuid4().hex,
        matter_id=str(matter.matter_id),
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        due_date=None,
        done_date=None,
        owner_staff_party_id="staff-mgmt",
        is_deleted=False,
    )
    db_session.add_all([matter, legacy_v2, operational_missing_due])
    db_session.commit()

    res = admin_client.get(
        "/admin/data-quality?sample=20&parse_days=30",
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
        },
    )
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert _extract_issue_count(html, "Dockets Missing Due Date") == 1
    assert _extract_issue_count(html, "Legacy V2_LIMIT Reference Dockets") == 1
    assert "LimitHistory:LeftMenu0001:test" in html


def test_admin_data_quality_page_reports_unresolved_legacy_case_only_links(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()

    unresolved_case = Case(ref_no=f"LEG-{uuid.uuid4().hex[:8]}", title="Legacy only case")
    resolved_ref = f"RES-{uuid.uuid4().hex[:8]}"
    resolved_case = Case(ref_no=resolved_ref, title="Resolvable legacy case")
    resolved_matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=resolved_ref,
        right_name="Resolvable Matter",
        matter_type="PATENT",
        created_at=datetime.utcnow(),
    )
    db_session.add_all([unresolved_case, resolved_case, resolved_matter])
    db_session.flush()

    unresolved_deadline = Deadline(
        case_id=unresolved_case.id,
        title="Unresolved legacy deadline",
        type="LEGAL",
        due_date=date(2026, 3, 1),
        status="new",
    )
    resolved_deadline = Deadline(
        case_id=resolved_case.id,
        title="Resolved legacy deadline",
        type="LEGAL",
        due_date=date(2026, 3, 1),
        status="new",
    )
    db_session.add_all([unresolved_deadline, resolved_deadline])
    db_session.commit()

    res = admin_client.get(
        "/admin/data-quality?sample=20&parse_days=30",
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
        },
    )
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert _extract_issue_count(html, "Unresolved Legacy Case Links") == 1
    assert "Unresolved legacy deadline" in html
    assert "Resolved legacy deadline" not in html


def test_data_quality_legacy_case_only_report_skips_missing_legacy_tables(
    app, db_session, monkeypatch
):
    from sqlalchemy.exc import ProgrammingError

    from app.services.admin import data_quality_service

    class MissingLegacyTableQuery:
        def count(self):
            raise ProgrammingError("SELECT 1", {}, Exception("missing legacy table"))

        def limit(self, _limit):
            return self

        def all(self):
            raise ProgrammingError("SELECT 1", {}, Exception("missing legacy table"))

    monkeypatch.setattr(
        data_quality_service,
        "_legacy_case_only_link_queries",
        lambda: (MissingLegacyTableQuery(),),
    )

    with app.app_context():
        metrics = data_quality_service.get_data_quality_metrics(sample_limit=5, parse_days=30)

    issues = {item["id"]: item for item in metrics["issues"]}
    assert issues["legacy_case_only_links"]["count"] == 0
    assert metrics["legacy_case_only_links"] == []
