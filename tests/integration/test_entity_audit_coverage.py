import json

from app.models.annuity import AnnuityItem
from app.models.audit_log import AuditLog
from app.models.docket import DocketItem
from app.models.matter_facts import MatterFacts
from app.models.party import Party, PartyStaff
from app.models.system_config import SystemConfig


def _audit(action: str, target_type: str):
    return (
        AuditLog.query.filter_by(action=action, target_type=target_type)
        .order_by(AuditLog.id.desc())
        .first()
    )


def test_api_deadline_create_writes_entity_audit(admin_client, db_session, sample_matter):
    resp = admin_client.post(
        f"/api/cases/{sample_matter.matter_id}/deadlines",
        json={"label": "Text Text Text", "due_date": "2026-06-01"},
    )

    assert resp.status_code == 201
    log = _audit("docket.create", "docket_item")
    assert log is not None
    meta = json.loads(log.meta_json)
    assert meta["matter_id"] == str(sample_matter.matter_id)
    assert meta["docket_id"] == resp.get_json()["id"]


def test_payable_create_writes_entity_audit(admin_client, db_session, sample_matter):
    resp = admin_client.post(
        f"/api/cases/{sample_matter.matter_id}/payables",
        json={
            "expense_id": "pay-audit-1",
            "requested_total": "120000",
            "currency": "USD",
            "vendor_name": "Text Text",
        },
    )

    assert resp.status_code == 201
    expense_id = resp.get_json()["expense_id"]
    log = _audit("expense.create", "expense")
    assert log is not None
    meta = json.loads(log.meta_json)
    assert meta["expense_id"] == expense_id
    assert meta["matter_id"] == str(sample_matter.matter_id)


def test_admin_config_update_writes_entity_audit(admin_client, db_session):
    db_session.add(SystemConfig(key="AUDIT_TEST_FLAG", value="old"))
    db_session.commit()

    resp = admin_client.post("/admin/api/config", json={"AUDIT_TEST_FLAG": "new"})

    assert resp.status_code == 200
    log = _audit("admin.config.update", "system_config")
    assert log is not None
    meta = json.loads(log.meta_json)
    assert meta["key"] == "AUDIT_TEST_FLAG"
    assert meta["changes"]["value"] == {"from": "old", "to": "new"}


def test_admin_staff_reassign_writes_aggregate_and_docket_audit(
    admin_client, db_session, sample_matter
):
    source_id = "staff-audit-source"
    target_id = "staff-audit-target"
    db_session.add_all(
        [
            Party(party_id=source_id, name_display="Text Text"),
            PartyStaff(party_id=source_id, staff_code="SRC", dept="QA", active=1),
            Party(party_id=target_id, name_display="Text Text"),
            PartyStaff(party_id=target_id, staff_code="DST", dept="QA", active=1),
            DocketItem(
                docket_id="docket-audit-reassign",
                matter_id=str(sample_matter.matter_id),
                category="WORK",
                name_free="Text Text Text",
                due_date="2026-06-10",
                owner_staff_party_id=source_id,
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.post(
        f"/admin/api/staff/{source_id}/reassign",
        json={"target_party_id": target_id},
    )

    assert resp.status_code == 200
    row = db_session.get(DocketItem, "docket-audit-reassign")
    assert row.owner_staff_party_id == target_id

    aggregate_log = _audit("admin.staff.reassign", "staff")
    assert aggregate_log is not None
    aggregate_meta = json.loads(aggregate_log.meta_json)
    assert aggregate_meta["counts"]["docket_items"] == 1

    docket_log = _audit("docket.update", "docket_item")
    assert docket_log is not None
    docket_meta = json.loads(docket_log.meta_json)
    assert docket_meta["docket_id"] == "docket-audit-reassign"
    assert docket_meta["changes"]["owner_staff_party_id"] == {
        "from": source_id,
        "to": target_id,
    }


def test_renewal_status_patch_writes_entity_audit(admin_client, db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.our_ref = "26TD0001US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    db_session.add(matter)
    annuity = AnnuityItem(
        annuity_id="annuity-audit-1",
        matter_id=str(sample_matter.matter_id),
        cycle_no=10,
        annuity_status="pending",
        due_date="2026-07-01",
    )
    db_session.add(annuity)
    db_session.add(MatterFacts(matter_id=str(sample_matter.matter_id), right_type_norm="TRADEMARK"))
    db_session.commit()

    resp = admin_client.patch(
        f"/renewal/api/fees/{annuity.annuity_id}",
        json={"status": "paid", "paid_date": "2026-05-21"},
    )

    assert resp.status_code == 200
    log = _audit("annuity.status_change", "annuity_item")
    assert log is not None
    meta = json.loads(log.meta_json)
    assert meta["annuity_id"] == "annuity-audit-1"
    assert meta["title"] == "Section 8/9 Renewal"
    assert meta["changes"]["annuity_status"] == {"from": "pending", "to": "paid"}
