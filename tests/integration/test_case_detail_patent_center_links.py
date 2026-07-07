from __future__ import annotations

import uuid
from pathlib import Path


def _create_patent_case_with_identifiers(db_session, sample_user) -> str:
    from app.models.ip_records import Matter, MatterIdentifier, MatterStaffAssignment, VMatterOverview

    sample_user = db_session.merge(sample_user)
    if not (sample_user.staff_party_id or "").strip():
        sample_user.staff_party_id = f"staff_{uuid.uuid4().hex[:8]}"
        db_session.add(sample_user)
        db_session.flush()

    matter_id = uuid.uuid4().hex
    our_ref = f"TEST-PC-LINK-{matter_id[:8]}"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Patent Center link test",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Patent Center link test",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add_all(
        [
            MatterIdentifier(
                matter_id=matter_id,
                id_type="Application No.",
                id_value="17/413,405",
            ),
            MatterIdentifier(
                matter_id=matter_id,
                id_type="Registration No.",
                id_value="12,345,678",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=sample_user.staff_party_id,
                staff_role_code="attorney",
            ),
        ]
    )
    db_session.commit()
    return matter_id


def test_case_detail_renders_current_patent_center_application_link(
    authenticated_client,
    db_session,
    sample_user,
):
    matter_id = _create_patent_case_with_identifiers(db_session, sample_user)

    resp = authenticated_client.get(f"/case/{matter_id}")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    assert "https://patentcenter.uspto.gov/applications/17413405/ifw/docs" in html
    assert "https://patentcenter.uspto.gov/search" in html
    assert "patentcenter.uspto.gov/ _" not in html
    assert "argRadSel01" not in html
    assert "recordCountPerPage" not in html


def test_legacy_case_view_template_removed():
    assert not Path("app/templates/case/view.html").exists()
