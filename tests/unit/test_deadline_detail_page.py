from __future__ import annotations

import json
import re
import uuid
from datetime import date

from bs4 import BeautifulSoup

from app.models.audit_log import AuditLog
from app.models.docket import DocketItem
from app.models.party import Party
from app.models.ip_records import MatterPartyRole
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def _staff_party_id(db_session, sample_user) -> str:
    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    user = db_session.get(User, user_id)
    return str(getattr(user, "staff_party_id", "") or "").strip()


def test_deadline_detail_page_renders_enriched_context(
    authenticated_client, db_session, sample_matter, sample_user
):
    matter_id = _matter_id(sample_matter)
    staff_pid = _staff_party_id(db_session, sample_user)
    owner_name = "Text Text"
    applicant_name = "Text Text"
    docket_id = uuid.uuid4().hex

    db_session.add(Party(party_id=staff_pid, name_display=owner_name))
    db_session.add(Party(party_id="APPLICANT-1", name_display=applicant_name))
    db_session.add(
        MatterPartyRole(
            matter_id=matter_id,
            party_id="APPLICANT-1",
            role_code="applicant",
        )
    )
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="MGMT_WORK",
            name_ref="MGMT:STATUS_RED:Text",
            name_free="Text",
            due_date="2026-03-15",
            visible_from_date="2026-01-15",
            owner_staff_party_id=staff_pid,
            snapshot_attorney=owner_name,
            snapshot_manager="Text",
            memo=json.dumps(
                {
                    "auto": True,
                    "trigger": "status_red",
                    "status_red": "Text",
                    "policy_id": "FOREIGN_FILING_PARIS_MAIN",
                    "deadline_code": "FOREIGN_FILING_PARIS",
                },
                ensure_ascii=False,
            ),
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="Text",
            status="Pending",
            business_code=f"DOCKET:{docket_id}",
            due_date=date(2026, 3, 15),
        )
    )
    db_session.add(
        WorkLog(
            docket_id=docket_id,
            matter_id=matter_id,
            task_name="Text",
            status="pending",
            owner_staff_party_id=staff_pid,
            due_date=date(2026, 3, 15),
            description="Text Text",
        )
    )
    db_session.add(
        AuditLog(
            actor_id=getattr(sample_user, "_test_id", None) or sample_user.id,
            user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
            action="docket.update",
            target_type="docket_item",
            meta_json=json.dumps(
                {
                    "docket_id": docket_id,
                    "matter_id": matter_id,
                    "changes": {
                        "internal_due_date": {"from": None, "to": "2026-03-10"},
                    },
                },
                ensure_ascii=False,
            ),
        )
    )
    db_session.commit()

    resp = authenticated_client.get(f"/deadline/item/{docket_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    page_text = soup.get_text(" ", strip=True)

    assert "Text Text" in page_text
    assert "Text Text Text" in page_text
    assert "Text Text Text Text" in page_text
    assert "Text Text" in page_text
    assert "FOREIGN_FILING_PARIS_MAIN" in page_text
    assert owner_name in page_text
    assert applicant_name in page_text
    assert "Text Text" in page_text
    assert "Text Text" in page_text
    assert "/Task" in page_text
    assert "MGMT_WORK" in page_text
    assert "2026-03-10" in page_text

    workflow_link = soup.find("a", href=lambda href: bool(href and "/workflow/" in href))
    assert workflow_link is not None
    assert "/workflow/" in (workflow_link.get("href") or "")


def test_deadline_detail_page_marks_legacy_reference_rows(
    authenticated_client, db_session, sample_matter, sample_user
):
    matter_id = _matter_id(sample_matter)
    staff_pid = _staff_party_id(db_session, sample_user)
    docket_id = uuid.uuid4().hex

    db_session.add(Party(party_id=staff_pid, name_display="Text Text"))
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="V2_LIMIT",
            name_free="Text",
            owner_staff_party_id=staff_pid,
            raw_id="LimitHistory:LeftMenu0001:1189::",
        )
    )
    db_session.commit()

    resp = authenticated_client.get(f"/deadline/item/{docket_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    page_text = soup.get_text(" ", strip=True)
    assert re.search(r"Legacy\s+Docket", page_text)
    assert "Legacy LimitHistory" in page_text
    assert "V2_LIMIT" in page_text
