from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest


def _create_pct_with_related_priority(db_session):
    from app.models.ip_records import (
        Family,
        Matter,
        MatterCustomField,
        MatterEvent,
        MatterFamily,
        MatterIdentifier,
        VMatterOverview,
    )

    pct = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="26PD0001PCT",
        right_name="PCT target",
        right_group="ETC",
        matter_type="PCT",
        status_blue="Text",
        is_deleted=False,
    )
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="24PD0001US",
        right_name="Priority source",
        right_group="DOM",
        matter_type="PATENT",
        status_blue="Text",
        is_deleted=False,
    )
    fam = Family(family_id=uuid.uuid4().hex, family_key="FAM-PCT-RELATED")
    db_session.add_all([pct, source, fam])
    db_session.flush()

    db_session.add_all(
        [
            VMatterOverview(
                matter_id=pct.matter_id,
                our_ref=pct.our_ref,
                right_name=pct.right_name,
                right_group=pct.right_group,
                matter_type=pct.matter_type,
                status_blue=pct.status_blue,
            ),
            VMatterOverview(
                matter_id=source.matter_id,
                our_ref=source.our_ref,
                right_name=source.right_name,
                right_group=source.right_group,
                matter_type=source.matter_type,
                status_blue=source.status_blue,
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(pct.matter_id),
                family_id=str(fam.family_id),
                link_role="manual",
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(source.matter_id),
                family_id=str(fam.family_id),
                link_role="manual",
            ),
            MatterIdentifier(
                matter_id=str(source.matter_id),
                id_type="Application No.",
                id_value="10-2024-0001234",
            ),
            MatterEvent(
                matter_id=str(source.matter_id),
                event_key="Filing date",
                event_at="2024-01-15",
                raw_text="2024-01-15",
                source_column="test",
            ),
            MatterCustomField(matter_id=str(pct.matter_id), namespace="pct", data={}),
        ]
    )
    db_session.commit()
    return pct, source


def _create_special_with_related_priority(
    db_session,
    *,
    target_type: str,
    target_ref: str,
    target_title: str,
    source_type: str,
    source_ref: str,
    source_title: str,
    namespace: str,
    source_application_date: str = "2024-01-15",
):
    from app.models.ip_records import (
        Family,
        Matter,
        MatterCustomField,
        MatterEvent,
        MatterFamily,
        MatterIdentifier,
        VMatterOverview,
    )

    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=target_ref,
        right_name=target_title,
        right_group="ETC",
        matter_type=target_type,
        status_blue="Text",
        is_deleted=False,
    )
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=source_ref,
        right_name=source_title,
        right_group="DOM",
        matter_type=source_type,
        status_blue="Text",
        is_deleted=False,
    )
    fam = Family(family_id=uuid.uuid4().hex, family_key=f"FAM-{target_type}-RELATED")
    db_session.add_all([target, source, fam])
    db_session.flush()

    db_session.add_all(
        [
            VMatterOverview(
                matter_id=target.matter_id,
                our_ref=target.our_ref,
                right_name=target.right_name,
                right_group=target.right_group,
                matter_type=target.matter_type,
                status_blue=target.status_blue,
            ),
            VMatterOverview(
                matter_id=source.matter_id,
                our_ref=source.our_ref,
                right_name=source.right_name,
                right_group=source.right_group,
                matter_type=source.matter_type,
                status_blue=source.status_blue,
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(target.matter_id),
                family_id=str(fam.family_id),
                link_role="manual",
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(source.matter_id),
                family_id=str(fam.family_id),
                link_role="manual",
            ),
            MatterIdentifier(
                matter_id=str(source.matter_id),
                id_type="Application No.",
                id_value="10-2024-0001234",
            ),
            MatterEvent(
                matter_id=str(source.matter_id),
                event_key="Filing date",
                event_at=source_application_date,
                raw_text=source_application_date,
                source_column="test",
            ),
            MatterCustomField(matter_id=str(target.matter_id), namespace=namespace, data={}),
        ]
    )
    db_session.commit()
    return target, source


def test_family_context_suggests_pct_related_application_fields(db_session):
    from app.blueprints.case.services.detail_context import _build_base, _build_family_section

    pct, source = _create_pct_with_related_priority(db_session)

    ctx = _build_base(str(pct.matter_id), {}, None)
    ctx["_current_user"] = None
    family = _build_family_section(ctx)

    suggestion = family.get("pct_related_application_suggestion")
    assert suggestion is not None
    assert suggestion["source"]["matter_id"] == str(source.matter_id)
    assert suggestion["source"]["application_date"] == "2024-01-15"
    assert {field["key"] for field in suggestion["fields"]} == {
        "priority_date",
        "priority_no",
        "national_phase_19m_deadline",
        "national_phase_deadline",
    }
    field_values = {field["key"]: field["value"] for field in suggestion["fields"]}
    assert field_values["national_phase_19m_deadline"] == "2025-07-15"
    assert field_values["national_phase_deadline"] == "2026-07-15"


def test_family_context_ignores_deleted_related_application_source(db_session):
    from app.blueprints.case.services.detail_context import _build_base, _build_family_section

    pct, source = _create_pct_with_related_priority(db_session)
    source.is_deleted = True
    db_session.add(source)
    db_session.commit()

    ctx = _build_base(str(pct.matter_id), {}, None)
    ctx["_current_user"] = None
    family = _build_family_section(ctx)

    assert family.get("related_family_rows") == []
    assert family.get("pct_related_application_suggestion") is None
    assert family.get("related_application_suggestion") is None


def test_related_application_suggestion_respects_explicit_empty_custom_data(db_session):
    from app.blueprints.case.services.detail_context import _build_base, _build_family_section
    from app.models.ip_records import MatterCustomField
    from app.services.matter.pct_related_application import build_related_application_suggestion

    pct, _source = _create_pct_with_related_priority(db_session)
    row = MatterCustomField.query.filter_by(matter_id=str(pct.matter_id), namespace="pct").first()
    assert row is not None
    row.data = {"priority_date": "2024-02-01"}
    db_session.commit()

    ctx = _build_base(str(pct.matter_id), {}, None)
    ctx["_current_user"] = None
    family = _build_family_section(ctx)
    suggestion = build_related_application_suggestion(
        matter=pct,
        related_family_rows=family.get("related_family_rows"),
        custom_data={},
    )

    assert suggestion is not None
    field_values = {field["key"]: field["value"] for field in suggestion["fields"]}
    assert field_values["priority_date"] == "2024-01-15"
    assert field_values["national_phase_deadline"] == "2026-07-15"


def test_pct_related_application_apply_fills_pct_fields(admin_client, db_session):
    from app.models.ip_records import MatterCustomField, MatterEvent, MatterIdentifier

    pct, _source = _create_pct_with_related_priority(db_session)

    resp = admin_client.post(
        f"/case/{pct.matter_id}/pct-related-application/apply",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert f"/case/{pct.matter_id}#sec-family" in (resp.headers.get("Location") or "")

    row = MatterCustomField.query.filter_by(
        matter_id=str(pct.matter_id),
        namespace="pct",
    ).first()
    assert row is not None
    assert row.data["priority_date"] == "2024-01-15"
    assert row.data["priority_no"] == "10-2024-0001234"
    assert row.data["national_phase_19m_deadline"] == "2025-07-15"
    assert row.data["national_phase_deadline"] == "2026-07-15"

    priority_identifier = MatterIdentifier.query.filter_by(
        matter_id=str(pct.matter_id),
        id_type="Priority",
    ).first()
    assert priority_identifier is not None
    assert priority_identifier.id_value == "10-2024-0001234"

    events = {
        event.event_key: event.event_at
        for event in MatterEvent.query.filter_by(matter_id=str(pct.matter_id)).all()
    }
    assert events["PRIORITY_DATE"] == "2024-01-15"
    assert events["Domestic Deadline 1  Notice"] == "2025-07-15"
    assert events["Domestic Due date"] == "2026-07-15"


def test_related_application_apply_requires_edit_not_family_view(
    authenticated_client, sample_user, db_session
):
    from app.models.ip_records import MatterCustomField, MatterStaffAssignment
    from app.utils.permissions import can_access_matter

    pct, source = _create_pct_with_related_priority(db_session)
    user = db_session.merge(sample_user)
    if not (user.staff_party_id or "").strip():
        user.staff_party_id = f"STAFF-RELATED-{uuid.uuid4().hex[:8]}"
    db_session.add(user)
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(source.matter_id),
            staff_party_id=str(user.staff_party_id),
            staff_role_code="attorney",
        )
    )
    db_session.commit()

    assert can_access_matter(user, str(pct.matter_id), action="view") is True
    assert can_access_matter(user, str(pct.matter_id), action="edit_case") is False

    resp = authenticated_client.post(
        f"/case/{pct.matter_id}/related-application/apply",
        follow_redirects=False,
    )
    assert resp.status_code == 403

    row = MatterCustomField.query.filter_by(
        matter_id=str(pct.matter_id),
        namespace="pct",
    ).first()
    assert row is not None
    assert row.data == {}


@pytest.mark.parametrize(
    ("target_type", "target_key", "source_type", "namespace", "route_value"),
    [
        ("MADRID", "madrid", "TRADEMARK", "outgoing_trademark", "Madrid"),
        ("HAGUE", "hague", "DESIGN", "outgoing_design", "HAGUE"),
    ],
)
def test_family_context_suggests_protocol_related_application_fields(
    db_session,
    target_type,
    target_key,
    source_type,
    namespace,
    route_value,
):
    from app.blueprints.case.services.detail_context import _build_base, _build_family_section

    target, source = _create_special_with_related_priority(
        db_session,
        target_type=target_type,
        target_ref=f"26SP{uuid.uuid4().hex[:4].upper()}{target_type}",
        target_title=f"{target_type} target",
        source_type=source_type,
        source_ref=f"24SP{uuid.uuid4().hex[:4].upper()}US",
        source_title=f"{source_type} priority source",
        namespace=namespace,
    )

    ctx = _build_base(str(target.matter_id), {}, None)
    ctx["_current_user"] = None
    family = _build_family_section(ctx)

    suggestion = family.get("related_application_suggestion")
    assert suggestion is not None
    assert suggestion["target"] == target_key
    assert family.get("pct_related_application_suggestion") is None
    assert suggestion["source"]["matter_id"] == str(source.matter_id)
    assert suggestion["source"]["application_date"] == "2024-01-15"
    assert {field["key"] for field in suggestion["fields"]} == {
        "app_route",
        "priority_claimed",
        "priority_date",
        "priority_no",
        "filing_deadline_type",
        "filing_deadline",
    }
    field_values = {field["key"]: field["value"] for field in suggestion["fields"]}
    assert field_values["app_route"] == route_value
    assert field_values["priority_date"] == "2024-01-15"
    assert field_values["priority_no"] == "10-2024-0001234"
    assert field_values["filing_deadline_type"] == "LEGAL"
    assert field_values["filing_deadline"] == "2024-07-15"


@pytest.mark.parametrize(
    (
        "target_type",
        "source_type",
        "namespace",
        "route_value",
        "protocol_no_key",
        "protocol_date_event",
    ),
    [
        (
            "MADRID",
            "TRADEMARK",
            "outgoing_trademark",
            "Madrid",
            "madrid_application_no",
            " Filing date",
        ),
        (
            "HAGUE",
            "DESIGN",
            "outgoing_design",
            "HAGUE",
            "hague_application_no",
            " Filing date",
        ),
    ],
)
def test_protocol_related_application_apply_fills_priority_not_protocol_application_fields(
    admin_client,
    db_session,
    target_type,
    source_type,
    namespace,
    route_value,
    protocol_no_key,
    protocol_date_event,
):
    from app.models.ip_records import MatterCustomField, MatterEvent, MatterIdentifier

    target, _source = _create_special_with_related_priority(
        db_session,
        target_type=target_type,
        target_ref=f"26AP{uuid.uuid4().hex[:4].upper()}{target_type}",
        target_title=f"{target_type} target",
        source_type=source_type,
        source_ref=f"24AP{uuid.uuid4().hex[:4].upper()}US",
        source_title=f"{source_type} priority source",
        namespace=namespace,
    )

    resp = admin_client.post(
        f"/case/{target.matter_id}/related-application/apply",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert f"/case/{target.matter_id}#sec-family" in (resp.headers.get("Location") or "")

    row = MatterCustomField.query.filter_by(
        matter_id=str(target.matter_id),
        namespace=namespace,
    ).first()
    assert row is not None
    assert row.data["app_route"] == route_value
    assert row.data["priority_claimed"] == "Y"
    assert row.data["priority_date"] == "2024-01-15"
    assert row.data["priority_no"] == "10-2024-0001234"
    assert row.data["filing_deadline_type"] == "LEGAL"
    assert row.data["filing_deadline"] == "2024-07-15"
    assert protocol_no_key not in row.data

    priority_identifier = MatterIdentifier.query.filter_by(
        matter_id=str(target.matter_id),
        id_type="Priority",
    ).first()
    assert priority_identifier is not None
    assert priority_identifier.id_value == "10-2024-0001234"

    events = {
        event.event_key: event.event_at
        for event in MatterEvent.query.filter_by(matter_id=str(target.matter_id)).all()
    }
    assert events["PRIORITY_DATE"] == "2024-01-15"
    assert events["Filing deadline"] == "2024-07-15"
    assert protocol_date_event not in events


@pytest.mark.parametrize(
    ("target_type", "source_type", "namespace", "route_value"),
    [
        ("MADRID", "TRADEMARK", "outgoing_trademark", "Madrid"),
        ("HAGUE", "DESIGN", "outgoing_design", "HAGUE"),
    ],
)
def test_protocol_related_application_apply_syncs_filing_docket_and_auto_status(
    admin_client,
    db_session,
    target_type,
    source_type,
    namespace,
    route_value,
):
    from app.models.ip_records import DocketItem, Matter

    from app.services.matter.pct_related_application import add_months

    source_date = (date.today() + timedelta(days=45)).isoformat()
    filing_deadline = add_months(date.fromisoformat(source_date), 6).isoformat()
    target, _source = _create_special_with_related_priority(
        db_session,
        target_type=target_type,
        target_ref=f"26AS{uuid.uuid4().hex[:4].upper()}{target_type}",
        target_title=f"{target_type} auto status target",
        source_type=source_type,
        source_ref=f"24AS{uuid.uuid4().hex[:4].upper()}US",
        source_title=f"{source_type} priority source",
        namespace=namespace,
        source_application_date=source_date,
    )

    resp = admin_client.post(
        f"/case/{target.matter_id}/related-application/apply",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    docket = DocketItem.query.filter_by(
        matter_id=str(target.matter_id),
        name_ref="Filing",
    ).first()
    assert docket is not None
    assert docket.due_date == filing_deadline
    assert (docket.extended_due_date or "") == ""
    assert not (docket.done_date or "").strip()

    refreshed = Matter.query.get(str(target.matter_id))
    assert refreshed is not None
    assert refreshed.status_red == "FilingDeadline"
    assert refreshed.status_red_related_date == filing_deadline
    assert refreshed.status_blue == "Filing  In Progress"
