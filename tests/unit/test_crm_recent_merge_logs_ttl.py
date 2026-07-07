import json
from datetime import datetime, timedelta


def test_crm_recent_merge_logs_respects_ttl(admin_client, db_session, monkeypatch):
    from app.models.crm_client_merge_log import CRMClientMergeLog

    monkeypatch.setitem(admin_client.application.config, "CRM_RECENT_MERGE_LOG_TTL_SECONDS", 3600)

    now = datetime.utcnow()
    old_log = CRMClientMergeLog(
        target_client_id=1111,
        source_client_ids_json=json.dumps([1, 2]),
        payload_json="{}",
        created_at=now - timedelta(hours=2),
    )
    recent_log = CRMClientMergeLog(
        target_client_id=2222,
        source_client_ids_json=json.dumps([3]),
        payload_json="{}",
        created_at=now - timedelta(minutes=5),
    )
    db_session.add_all([old_log, recent_log])
    db_session.commit()

    resp = admin_client.get("/crm/")
    html = resp.data.decode("utf-8")

    assert "#2222" in html
    assert "#1111" not in html
