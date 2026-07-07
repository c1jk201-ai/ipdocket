from app.models.assets import FileAsset
from app.models.communication import Communication, CommunicationFileAsset
from app.models.docket import DocketItem
from app.models.matter import Matter, MatterCustomField
from app.models.matter_status_recalc_queue import MatterStatusRecalcQueue
from app.services.matter import matter_status_recalc_queue as matter_status_recalc_queue_service
from app.services.matter.matter_status_recalc_listeners import init_matter_status_recalc_listeners
from app.services.matter.matter_status_recalc_queue import (
    drain_matter_status_recalc_queue,
    enqueue_matter_status_recalc,
)


def _make_outgoing_patent(
    db_session,
    *,
    our_ref: str,
    status_blue: str,
    status_red: str = "Text",
    status_red_related_date: str = "2026-05-01",
    payload: dict | None = None,
) -> Matter:
    matter = Matter(
        matter_id=our_ref.lower(),
        our_ref=our_ref,
        right_name="Text Text",
        right_group="OUT",
        matter_type="PATENT",
        status_red=status_red,
        status_red_related_date=status_red_related_date,
        status_blue=status_blue,
    )
    db_session.add(matter)
    db_session.add(
        MatterCustomField(
            matter_id=matter.matter_id,
            namespace="outgoing_patent",
            data=payload
            or {
                "exam_requested": "Y",
                "filing_deadline": status_red_related_date,
                "application_date": "",
                "exam_request_date": "",
            },
        )
    )
    db_session.commit()
    db_session.refresh(matter)
    return matter


def test_matter_status_recalc_queue_drain_updates_stale_matter(app, db_session):
    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POQ001US",
        status_blue="Text Text Text",
    )

    enqueue_matter_status_recalc(matter.matter_id, reason="unit_test")
    db_session.commit()

    queued = db_session.get(MatterStatusRecalcQueue, matter.matter_id)
    assert queued is not None

    result = drain_matter_status_recalc_queue(limit=10)

    db_session.refresh(matter)

    assert result == {"processed": 1, "updated": 1, "failed": 0}
    assert matter.status_blue == "Text Text Text"
    assert db_session.get(MatterStatusRecalcQueue, matter.matter_id) is None


def test_matter_status_recalc_queue_preserves_reenqueue_during_active_lock(
    app, db_session, monkeypatch
):
    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POQ008US",
        status_blue="Text Text Text",
    )

    enqueue_matter_status_recalc(matter.matter_id, reason="initial")
    db_session.commit()
    first_payload = db_session.get(MatterStatusRecalcQueue, matter.matter_id).payload

    class _Result:
        changed = False

    def _requeue_while_processing(*, matter, **_kwargs):
        enqueue_matter_status_recalc(matter.matter_id, reason="source_changed_again")
        return _Result()

    monkeypatch.setattr(
        matter_status_recalc_queue_service,
        "apply_auto_status_cache_to_matter",
        _requeue_while_processing,
    )

    result = drain_matter_status_recalc_queue(limit=1)

    queued = db_session.get(MatterStatusRecalcQueue, matter.matter_id)
    assert result == {"processed": 1, "updated": 0, "failed": 0}
    assert queued is not None
    assert queued.lock_token is None
    assert queued.locked_at is None
    assert queued.attempts == 0
    assert queued.payload != first_payload


def test_matter_status_recalc_listener_enqueues_and_drains_after_commit(
    app, db_session, monkeypatch
):
    from app.services.ops.background import BackgroundService

    init_matter_status_recalc_listeners()
    captured = []

    def _run_now(func, *args, **kwargs):
        captured.append((func, args, dict(kwargs)))
        return None

    monkeypatch.setattr(BackgroundService, "run_async", _run_now)

    matter = Matter(
        matter_id="26poq002us",
        our_ref="26POQ002US",
        right_name="Text Text",
        right_group="OUT",
        matter_type="PATENT",
        status_red="Text",
        status_red_related_date="2026-05-01",
        status_blue="Text Text Text",
    )
    db_session.add(matter)
    db_session.add(
        MatterCustomField(
            matter_id=matter.matter_id,
            namespace="outgoing_patent",
            data={
                "exam_requested": "Y",
                "filing_deadline": "2026-05-01",
                "application_date": "",
                "exam_request_date": "",
            },
        )
    )

    db_session.commit()
    db_session.refresh(matter)

    assert captured
    queued = db_session.get(MatterStatusRecalcQueue, matter.matter_id)
    assert queued is not None

    func, args, kwargs = captured[0]
    task_kwargs = dict(kwargs)
    task_kwargs.pop("_critical", None)
    task_kwargs.pop("_context", None)
    result = func(*args, **task_kwargs)
    db_session.refresh(matter)

    assert result == {"processed": 1, "updated": 1, "failed": 0}
    assert matter.status_blue == "Text Text Text"
    assert db_session.get(MatterStatusRecalcQueue, matter.matter_id) is None


def test_matter_status_recalc_listener_enqueues_notice_send_docket(app, db_session, monkeypatch):
    from app.services.ops.background import BackgroundService

    init_matter_status_recalc_listeners()
    captured = []

    monkeypatch.setattr(
        BackgroundService,
        "run_async",
        lambda func, *args, **kwargs: captured.append((func, args, dict(kwargs))),
    )

    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POQ003US",
        status_blue="Text Text Text",
    )

    db_session.add(
        DocketItem(
            docket_id="docket_notice_send",
            matter_id=matter.matter_id,
            category="MGMT",
            name_ref="MGMT:NOTICE_SEND_3D:oa123",
            name_free="Text Text(3Text Text)",
            due_date="2026-05-03",
        )
    )
    db_session.commit()

    assert captured
    assert db_session.get(MatterStatusRecalcQueue, matter.matter_id) is not None


def test_matter_status_recalc_listener_enqueues_response_attachment_change(
    app, db_session, monkeypatch
):
    from app.services.ops.background import BackgroundService

    init_matter_status_recalc_listeners()
    captured = []

    monkeypatch.setattr(
        BackgroundService,
        "run_async",
        lambda func, *args, **kwargs: captured.append((func, args, dict(kwargs))),
    )

    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POQ004US",
        status_blue="Text Text Text",
    )

    db_session.add(
        Communication(
            comm_id="resp_comm_1",
            matter_id=matter.matter_id,
            comm_type="R",
            sent_date="2026-04-10",
        )
    )
    db_session.add(
        FileAsset(
            file_asset_id="file_asset_resp_1",
            file_path="responses/test-response.txt",
            original_name="test-response.txt",
            mime_type="text/plain",
        )
    )
    db_session.commit()

    captured.clear()
    db_session.add(
        CommunicationFileAsset(
            comm_file_id="comm_file_resp_1",
            comm_id="resp_comm_1",
            file_asset_id="file_asset_resp_1",
            role="attachment",
        )
    )
    db_session.commit()

    assert captured
    assert db_session.get(MatterStatusRecalcQueue, matter.matter_id) is not None


def test_matter_status_recalc_listener_enqueue_failure_does_not_break_commit(
    app, db_session, monkeypatch
):
    from app.services.ops.background import BackgroundService

    init_matter_status_recalc_listeners()
    captured = []

    monkeypatch.setattr(
        BackgroundService,
        "run_async",
        lambda func, *args, **kwargs: captured.append((func, args, dict(kwargs))),
    )

    original_execute = db_session.execute

    def _patched_execute(statement, *args, **kwargs):
        if "matter_status_recalc_queue" in str(statement):
            raise RuntimeError("simulated queue upsert failure")
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", _patched_execute)

    matter = Matter(
        matter_id="26poq005us",
        our_ref="26POQ005US",
        right_name="Text Text",
        right_group="OUT",
        matter_type="PATENT",
        status_red="Text",
        status_red_related_date="2026-05-01",
        status_blue="Text Text Text",
    )
    db_session.add(matter)
    db_session.add(
        MatterCustomField(
            matter_id=matter.matter_id,
            namespace="outgoing_patent",
            data={
                "exam_requested": "Y",
                "filing_deadline": "2026-05-01",
                "application_date": "",
                "exam_request_date": "",
            },
        )
    )

    db_session.commit()
    monkeypatch.setattr(db_session, "execute", original_execute)

    persisted = db_session.get(Matter, matter.matter_id)
    assert persisted is not None
    assert db_session.get(MatterStatusRecalcQueue, matter.matter_id) is None
    assert captured == []


def test_matter_status_recalc_queue_batch_drain_uses_token_scope_heartbeat(
    app, db_session, monkeypatch
):
    created = []

    class _FakeHeartbeat:
        def __init__(self, app_obj, **kwargs):
            self.kwargs = dict(kwargs)
            self.lost = False
            created.append(self.kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        matter_status_recalc_queue_service,
        "QueueLockHeartbeat",
        _FakeHeartbeat,
    )

    first = _make_outgoing_patent(
        db_session,
        our_ref="26POQ006US",
        status_blue="Text Text Text",
    )
    second = _make_outgoing_patent(
        db_session,
        our_ref="26POQ007US",
        status_blue="Text Text Text",
    )

    enqueue_matter_status_recalc(first.matter_id, reason="unit_test")
    enqueue_matter_status_recalc(second.matter_id, reason="unit_test")
    db_session.commit()

    with app.app_context():
        result = drain_matter_status_recalc_queue(limit=2)

    assert result == {"processed": 2, "updated": 2, "failed": 0}
    assert any(item.get("id_column") is None for item in created)
    assert all(item.get("token_column") == "lock_token" for item in created)
