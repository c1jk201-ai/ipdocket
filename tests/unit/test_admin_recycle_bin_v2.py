from __future__ import annotations

from datetime import datetime
import uuid

from app.models.assets import FileAsset, MatterFileAsset
from app.models.deletion_log import DeletionLog
from app.models.docket import DocketItem
from app.models.matter import Matter, MatterPartyRole
from app.models.party import Party
from app.models.legacy_finance import LegacyInvoice, LegacyInvoicePayment


def test_recycle_bin_preview_timeline_bulk_restore_and_permanent_delete(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref=f"RB-{uuid.uuid4().hex[:8]}",
        right_name="Recycle Bin Case",
        is_deleted=False,
    )
    docket_id_ok = uuid.uuid4().hex
    docket_id_blocked = uuid.uuid4().hex

    log_ok = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=docket_id_ok,
        title="Text Text Text",
        payload={
            "docket_id": docket_id_ok,
            "matter_id": matter_id,
            "name_ref": "OA_RESPONSE",
            "name_free": "OA Text",
            "due_date": "2026-03-01",
            "extended_due_date": "2026-02-20",
            "done_date": None,
            "owner_staff_party_id": None,
            "memo": "preview-ok",
        },
    )
    log_blocked = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=docket_id_blocked,
        title="Text Text Text",
        payload={
            "docket_id": docket_id_blocked,
            "matter_id": "missing-matter-id",
            "name_ref": "OA_BLOCKED",
            "name_free": "Text Text",
            "due_date": "2026-04-01",
            "extended_due_date": "2026-03-20",
            "done_date": None,
            "owner_staff_party_id": None,
            "memo": "preview-blocked",
        },
    )

    db_session.add_all([matter, log_ok, log_blocked])
    db_session.commit()
    log_ok_id = int(log_ok.id)
    log_blocked_id = int(log_blocked.id)

    preview_ok = admin_client.get(f"/admin/api/deletions/{log_ok_id}/preview", headers=req_headers)
    assert preview_ok.status_code == 200
    preview_ok_payload = preview_ok.get_json()
    assert preview_ok_payload["preview"]["can_restore"] is True
    assert preview_ok_payload["preview"]["blockers"] == []

    preview_blocked = admin_client.get(
        f"/admin/api/deletions/{log_blocked_id}/preview",
        headers=req_headers,
    )
    assert preview_blocked.status_code == 200
    preview_blocked_payload = preview_blocked.get_json()
    assert preview_blocked_payload["preview"]["can_restore"] is False
    assert len(preview_blocked_payload["preview"]["blockers"]) >= 1

    list_res = admin_client.get(
        "/admin/api/deletions",
        query_string={"matter": matter_id, "timeline": "1", "include_restored": "1"},
        headers=req_headers,
    )
    assert list_res.status_code == 200
    listed = list_res.get_json()
    listed_ids = {int(item["id"]) for item in listed.get("items", [])}
    assert log_ok_id in listed_ids
    assert listed.get("timeline")

    bulk_restore = admin_client.post(
        "/admin/api/deletions/bulk-restore",
        json={"ids": [log_ok_id, log_blocked_id]},
        headers=req_headers,
    )
    assert bulk_restore.status_code == 200
    restore_payload = bulk_restore.get_json()
    assert restore_payload["requested"] == 2
    assert restore_payload["restored"] == 1
    assert len(restore_payload.get("skipped", [])) >= 1

    db_session.expire_all()
    assert DocketItem.query.get(docket_id_ok) is not None
    assert DeletionLog.query.get(log_ok_id).restored_at is not None
    assert DeletionLog.query.get(log_blocked_id).restored_at is None

    bulk_delete = admin_client.post(
        "/admin/api/deletions/bulk-delete",
        json={"ids": [log_blocked_id]},
        headers=req_headers,
    )
    assert bulk_delete.status_code == 200
    delete_payload = bulk_delete.get_json()
    assert delete_payload["deleted"] == 1
    assert DeletionLog.query.get(log_blocked_id) is None


def test_recycle_bin_delete_all_matching_requires_confirmation(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    log = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Legacy cleanup protected",
        payload={"matter_id": uuid.uuid4().hex},
    )
    db_session.add(log)
    db_session.commit()
    log_id = int(log.id)

    res = admin_client.post(
        "/admin/api/deletions/bulk-delete",
        json={
            "delete_all_matching": True,
            "filters": {"entity_type": "deadline", "search": "Legacy cleanup"},
        },
        headers=req_headers,
    )

    assert res.status_code == 400
    assert DeletionLog.query.get(log_id) is not None


def test_recycle_bin_delete_all_matching_respects_current_filters(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    target_matter_id = uuid.uuid4().hex
    other_matter_id = uuid.uuid4().hex
    db_session.add_all(
        [
            Matter(
                matter_id=target_matter_id,
                our_ref=f"DELALL-{uuid.uuid4().hex[:8]}",
                right_name="Delete All Filter Target",
                is_deleted=False,
            ),
            Matter(
                matter_id=other_matter_id,
                our_ref=f"DELALL-{uuid.uuid4().hex[:8]}",
                right_name="Delete All Filter Other",
                is_deleted=False,
            ),
        ]
    )
    target_log = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Legacy cleanup target",
        parent_type="matter",
        parent_id=target_matter_id,
        payload={"matter_id": target_matter_id},
    )
    restored_log = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Legacy cleanup restored",
        parent_type="matter",
        parent_id=target_matter_id,
        restored_at=datetime.utcnow(),
        payload={"matter_id": target_matter_id},
    )
    other_type_log = DeletionLog(
        entity_type="workflow",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Legacy cleanup target",
        parent_type="matter",
        parent_id=target_matter_id,
        payload={"case_id": target_matter_id},
    )
    other_matter_log = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Legacy cleanup target",
        parent_type="matter",
        parent_id=other_matter_id,
        payload={"matter_id": other_matter_id},
    )
    db_session.add_all([target_log, restored_log, other_type_log, other_matter_log])
    db_session.commit()
    target_id = int(target_log.id)
    kept_ids = {int(restored_log.id), int(other_type_log.id), int(other_matter_log.id)}

    res = admin_client.post(
        "/admin/api/deletions/bulk-delete",
        json={
            "delete_all_matching": True,
            "confirm": "DELETE",
            "filters": {
                "entity_type": "deadline",
                "matter": target_matter_id,
                "search": "Legacy cleanup",
            },
        },
        headers=req_headers,
    )

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["mode"] == "all_matching"
    assert payload["deleted"] == 1
    assert payload["deleted_ids"] == [target_id]
    assert DeletionLog.query.get(target_id) is None
    assert {row.id for row in DeletionLog.query.filter(DeletionLog.id.in_(kept_ids)).all()} == kept_ids


def test_recycle_bin_matter_filter_respects_workflow_case_id(admin_client, db_session, monkeypatch):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    target_matter_id = uuid.uuid4().hex
    other_matter_id = uuid.uuid4().hex
    target_matter = Matter(
        matter_id=target_matter_id,
        our_ref=f"RB-{uuid.uuid4().hex[:8]}",
        right_name="Recycle Bin Filter",
        is_deleted=False,
    )
    other_matter = Matter(
        matter_id=other_matter_id,
        our_ref=f"RB-{uuid.uuid4().hex[:8]}",
        right_name="Recycle Bin Filter Other",
        is_deleted=False,
    )
    deadline_log = DeletionLog(
        entity_type="deadline",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Text",
        payload={"matter_id": target_matter_id},
    )
    workflow_log_match = DeletionLog(
        entity_type="workflow",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Text Text",
        payload={"case_id": target_matter_id},
    )
    workflow_log_non_match = DeletionLog(
        entity_type="workflow",
        entity_id=0,
        entity_key=uuid.uuid4().hex,
        title="Text Text",
        payload={"matter_id": target_matter_id, "case_id": other_matter_id},
    )
    db_session.add_all(
        [
            target_matter,
            other_matter,
            deadline_log,
            workflow_log_match,
            workflow_log_non_match,
        ]
    )
    db_session.commit()
    deadline_log_id = int(deadline_log.id)
    workflow_log_match_id = int(workflow_log_match.id)
    workflow_log_non_match_id = int(workflow_log_non_match.id)

    res = admin_client.get(
        "/admin/api/deletions",
        query_string={"matter": target_matter_id, "include_restored": "1", "timeline": "1"},
        headers=req_headers,
    )
    assert res.status_code == 200
    payload = res.get_json()
    ids = {int(item["id"]) for item in payload.get("items", [])}
    assert deadline_log_id in ids
    assert workflow_log_match_id in ids
    assert workflow_log_non_match_id not in ids


def test_auto_soft_delete_creates_deletion_log_with_parent_metadata(db_session):
    from datetime import datetime

    matter_id = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=f"AUTO-{uuid.uuid4().hex[:8]}",
            right_name="Auto Archive Case",
            is_deleted=False,
        )
    )
    docket = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Text Text Text",
        due_date="2026-05-01",
    )
    db_session.add(docket)
    db_session.commit()

    docket.is_deleted = True
    docket.deleted_at = datetime.utcnow()
    docket.deleted_by = 77
    db_session.commit()

    log = DeletionLog.query.filter_by(entity_type="deadline", entity_key=docket_id).first()
    assert log is not None
    assert log.parent_type == "matter"
    assert log.parent_id == matter_id
    assert "soft-delete" in (log.tags or "")
    assert "Text Text Text" in (log.search_vector or "")


def test_recycle_bin_restores_file_person_and_billing_invoice(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    matter_id = uuid.uuid4().hex
    file_asset_id = uuid.uuid4().hex
    matter_file_id = uuid.uuid4().hex
    party_id = uuid.uuid4().hex
    mpr_id = uuid.uuid4().hex
    invoice_id = uuid.uuid4().hex
    payment_id = uuid.uuid4().hex

    db_session.add_all(
        [
            Matter(
                matter_id=matter_id,
                our_ref=f"REST-{uuid.uuid4().hex[:8]}",
                right_name="Restore Extended Targets",
                is_deleted=False,
            ),
            FileAsset(
                file_asset_id=file_asset_id,
                storage_type="local",
                file_path="/tmp/test.pdf",
                original_name="test.pdf",
                is_deleted=False,
            ),
            Party(
                party_id=party_id,
                name_display="Text Text",
                party_kind="applicant",
            ),
        ]
    )

    file_log = DeletionLog(
        entity_type="file",
        entity_id=0,
        entity_key=matter_file_id,
        title="Text Text",
        parent_type="matter",
        parent_id=matter_id,
        payload={
            "_model": "MatterFileAsset",
            "matter_file_id": matter_file_id,
            "matter_id": matter_id,
            "file_asset_id": file_asset_id,
            "role": "document",
            "description": "Text Text Text",
            "is_deleted": True,
        },
    )
    person_log = DeletionLog(
        entity_type="person",
        entity_id=0,
        entity_key=mpr_id,
        title="Text Text",
        parent_type="matter",
        parent_id=matter_id,
        payload={
            "_model": "MatterPartyRole",
            "mpr_id": mpr_id,
            "matter_id": matter_id,
            "party_id": party_id,
            "role_code": "applicant",
            "seq": 1,
            "raw_text": "Text Text",
        },
    )
    billing_log = DeletionLog(
        entity_type="billing_invoice",
        entity_id=0,
        entity_key=invoice_id,
        title="Text Text",
        parent_type="matter",
        parent_id=matter_id,
        payload={
            "_model": "LegacyInvoice",
            "invoice_id": invoice_id,
            "matter_id": matter_id,
            "fee_ref": "FEE-RESTORE",
            "bill_date": "2026-05-01",
            "currency": "USD",
            "total_amount": 100000,
            "received_total": 100000,
            "outstanding_amount": 0,
            "status": "Text",
            "description": "Text Text Text",
            "is_deleted": True,
            "payments": [
                {
                    "_model": "LegacyInvoicePayment",
                    "payment_id": payment_id,
                    "invoice_id": invoice_id,
                    "installment_no": 1,
                    "paid_date": "2026-05-02",
                    "paid_amount": 100000,
                    "method": "bank",
                    "is_deleted": True,
                }
            ],
        },
    )
    db_session.add_all([file_log, person_log, billing_log])
    db_session.commit()

    res = admin_client.post(
        "/admin/api/deletions/bulk-restore",
        json={"ids": [file_log.id, person_log.id, billing_log.id]},
        headers=req_headers,
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["restored"] == 3

    db_session.expire_all()
    file_row = MatterFileAsset.query.get(matter_file_id)
    person_row = MatterPartyRole.query.get(mpr_id)
    invoice_row = LegacyInvoice.query.get(invoice_id)
    payment_row = LegacyInvoicePayment.query.get(payment_id)

    assert file_row is not None
    assert file_row.is_deleted is False
    assert person_row is not None
    assert person_row.party_id == party_id
    assert invoice_row is not None
    assert invoice_row.is_deleted is False
    assert payment_row is not None
    assert payment_row.is_deleted is False
