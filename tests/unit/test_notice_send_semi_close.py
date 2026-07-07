from __future__ import annotations

import json


def _state_from_memo(raw_memo: str | None) -> dict:
    payload = json.loads(raw_memo or "{}")
    state = payload.get("notice_send_semi_auto")
    return state if isinstance(state, dict) else {}


def test_parse_memo_payload_legacy_text_does_not_report_swallowed_exception(monkeypatch):
    from app.services.deadlines import notice_send_semi_close as svc

    called = {"count": 0}

    def _fake_report(*_args, **_kwargs):
        called["count"] += 1

    monkeypatch.setattr(svc, "report_swallowed_exception", _fake_report)

    payload, legacy_note = svc._parse_memo_payload("Text - Text Text: DocketItem Text")
    assert payload == {}
    assert legacy_note == "Text - Text Text: DocketItem Text"
    assert called["count"] == 0


def test_parse_memo_payload_malformed_json_reports_swallowed_exception(monkeypatch):
    from app.services.deadlines import notice_send_semi_close as svc

    called = {"count": 0}

    def _fake_report(*_args, **_kwargs):
        called["count"] += 1

    monkeypatch.setattr(svc, "report_swallowed_exception", _fake_report)

    payload, legacy_note = svc._parse_memo_payload("{not-json}")
    assert payload == {}
    assert legacy_note == "{not-json}"
    assert called["count"] == 1


def test_notice_send_candidate_prompt_and_ack_once(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        ack_notice_send_prompt,
        get_notice_send_prompt_candidate,
        mark_notice_send_candidates,
    )

    di = DocketItem(
        docket_id="semi-close-test-1",
        matter_id="matter-semi-close-1",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-1",
        name_free="Notice send task(3 days) - Office action notice",
        due_date="2026-03-01",
    )
    db_session.add(di)
    db_session.commit()

    marked = mark_notice_send_candidates(
        matter_id="matter-semi-close-1",
        direction="Send",
        doc_name="Office action notice",
        comm_type="M",
        source="unit_test",
        actor_user_id=7,
        candidate_date="2026-02-23",
    )
    assert marked == 1
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("prompted") is False
    assert state.get("trigger_doc_name") == "Office action notice"

    candidate = get_notice_send_prompt_candidate([di])
    assert candidate is not None
    assert candidate.get("docket_id") == "semi-close-test-1"

    acknowledged = ack_notice_send_prompt(
        matter_id="matter-semi-close-1",
        docket_id="semi-close-test-1",
        decision="no",
        actor_user_id=9,
        prompted_date="2026-02-23",
    )
    assert acknowledged is True
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("prompted") is True
    assert state.get("decision") == "no"
    assert state.get("prompted_by") == "9"
    assert get_notice_send_prompt_candidate([di]) is None

    # Once prompted, new outbound uploads must not re-open the popup,
    # but they should keep recommendation state current.
    marked_again = mark_notice_send_candidates(
        matter_id="matter-semi-close-1",
        direction="Send",
        doc_name="Office action notice",
        comm_type="M",
        source="unit_test_retrigger",
        actor_user_id=10,
    )
    assert marked_again == 1
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("prompted") is True
    assert state.get("trigger_doc_name") == "Office action notice"
    assert get_notice_send_prompt_candidate([di]) is None


def test_notice_send_candidate_skips_response_documents(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        get_notice_send_prompt_candidate,
        mark_notice_send_candidates,
    )

    di = DocketItem(
        docket_id="semi-close-test-2",
        matter_id="matter-semi-close-2",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-2",
        name_free="Notice send task(3 days) - Office action notice",
        due_date="2026-03-05",
    )
    db_session.add(di)
    db_session.commit()

    # Response-like doc title should not set candidate.
    marked_by_response_doc = mark_notice_send_candidates(
        matter_id="matter-semi-close-2",
        direction="Send",
        doc_name="OA response",
        comm_type="M",
        source="unit_test_response_doc",
    )
    assert marked_by_response_doc == 0

    # Response comm_type should also be ignored.
    marked_by_response_type = mark_notice_send_candidates(
        matter_id="matter-semi-close-2",
        direction="Send",
        doc_name="Office action notice",
        comm_type="R",
        source="unit_test_response_type",
    )
    assert marked_by_response_type == 0

    marked_by_telephone_type = mark_notice_send_candidates(
        matter_id="matter-semi-close-2",
        direction="Send",
        doc_name="Office action notice",
        comm_type="T",
        source="unit_test_telephone_type",
    )
    assert marked_by_telephone_type == 0

    db_session.commit()
    db_session.refresh(di)
    assert get_notice_send_prompt_candidate([di]) is None


def test_notice_send_candidate_requires_task_doc_affinity(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        get_notice_send_prompt_candidate,
        mark_notice_send_candidates,
    )

    di = DocketItem(
        docket_id="semi-close-test-3",
        matter_id="matter-semi-close-3",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-3",
        name_free="Notice send task(3 days) - Office action notice",
        due_date="2026-03-07",
    )
    db_session.add(di)
    db_session.commit()

    unrelated = mark_notice_send_candidates(
        matter_id="matter-semi-close-3",
        direction="Send",
        doc_name="Tax invoice",
        comm_type="M",
        source="unit_test_unrelated",
    )
    assert unrelated == 0

    related = mark_notice_send_candidates(
        matter_id="matter-semi-close-3",
        direction="Send",
        # Not an exact title match; should still pass by fuzzy affinity.
        doc_name="24TD0234US office action notice to client",
        comm_type="M",
        source="unit_test_related",
    )
    assert related == 1
    db_session.commit()
    db_session.refresh(di)
    assert get_notice_send_prompt_candidate([di]) is not None


def test_notice_send_candidate_matches_refund_notice_alias(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import mark_notice_send_candidates

    di = DocketItem(
        docket_id="semi-close-test-refund-1",
        matter_id="matter-semi-close-refund-1",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:refund-1",
        name_free="Notice send task(3 days) - Refund notice",
        due_date="2026-03-17",
    )
    db_session.add(di)
    db_session.commit()

    marked = mark_notice_send_candidates(
        matter_id="matter-semi-close-refund-1",
        direction="Send",
        doc_name="Official fee guidance for refund",
        sent_date="2026-03-17",
        comm_type="M",
        source="unit_test_refund_alias",
    )
    assert marked == 1
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("trigger_doc_name") == "Official fee guidance for refund"


def test_notice_send_candidate_refund_alias_does_not_match_other_notice(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import mark_notice_send_candidates

    di = DocketItem(
        docket_id="semi-close-test-refund-2",
        matter_id="matter-semi-close-refund-2",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:refund-2",
        name_free="Notice send task(3 days) - Refund notice",
        due_date="2026-03-17",
    )
    db_session.add(di)
    db_session.commit()

    marked = mark_notice_send_candidates(
        matter_id="matter-semi-close-refund-2",
        direction="Send",
        doc_name="Notice of allowance",
        sent_date="2026-03-17",
        comm_type="M",
        source="unit_test_refund_alias_negative",
    )
    assert marked == 0


def test_notice_send_candidate_matches_refund_notice_from_source_text(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import mark_notice_send_candidates

    di = DocketItem(
        docket_id="semi-close-test-refund-3",
        matter_id="matter-semi-close-refund-3",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:refund-3",
        name_free="Notice send task(3 days) - Refund notice",
        due_date="2026-03-17",
    )
    db_session.add(di)
    db_session.commit()

    marked = mark_notice_send_candidates(
        matter_id="matter-semi-close-refund-3",
        direction="Send",
        doc_name="Official fee guidance",
        source_text=(
            "Refund rate 85% and official fee guidance are included. "
            "Please confirm the refund notice."
        ),
        sent_date="2026-03-17",
        comm_type="M",
        source="unit_test_refund_source_text",
    )
    assert marked == 1
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("trigger_doc_name") == "Official fee guidance"


def test_notice_send_prompt_can_be_inferred_from_sent_history(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        get_notice_send_prompt_candidate,
        infer_notice_send_prompt_candidate_from_communications,
    )

    di = DocketItem(
        docket_id="semi-close-test-4",
        matter_id="matter-semi-close-4",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-4",
        name_free="Notice send task(3 days) - Office action notice",
        due_date="2026-03-09",
    )
    db_session.add(di)
    db_session.commit()

    # Candidate flag is missing, so the persisted prompt should not appear.
    assert get_notice_send_prompt_candidate([di]) is None

    inferred = infer_notice_send_prompt_candidate_from_communications(
        docket_items=[di],
        communications=[
            {
                "doc_name": "Office action notice to client",
                "comm_type": "M",
                "sent_date": "2026-02-23",
            }
        ],
    )
    assert inferred is not None
    assert inferred.get("docket_id") == "semi-close-test-4"
    assert inferred.get("inferred") is True


def test_notice_send_prompt_can_be_inferred_from_english_refusal_alias(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        infer_notice_send_prompt_candidate_from_communications,
    )

    di = DocketItem(
        docket_id="semi-close-test-refusal-1",
        matter_id="matter-semi-close-refusal-1",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:refusal-1",
        name_free="Notice send task(3 days) - Notice of Refusal",
        due_date="2026-03-19",
        memo=json.dumps({"received_date": "2026-03-17"}, ensure_ascii=False),
    )
    db_session.add(di)
    db_session.commit()

    inferred = infer_notice_send_prompt_candidate_from_communications(
        docket_items=[di],
        communications=[
            {
                "doc_name": "Notice of Refusal for Number of the international registration",
                "comm_type": "M",
                "sent_date": "2026-03-18",
            }
        ],
    )
    assert inferred is not None
    assert inferred.get("docket_id") == "semi-close-test-refusal-1"
    assert inferred.get("matched_doc_name") == (
        "Notice of Refusal for Number of the international registration"
    )


def test_notice_send_ack_allows_inferred_prompt_without_candidate(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        ack_notice_send_prompt,
        get_notice_send_prompt_candidate,
    )

    di = DocketItem(
        docket_id="semi-close-test-5",
        matter_id="matter-semi-close-5",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-5",
        name_free="Text Text(3Text Text) · Text",
        due_date="2026-03-10",
    )
    db_session.add(di)
    db_session.commit()

    acknowledged = ack_notice_send_prompt(
        matter_id="matter-semi-close-5",
        docket_id="semi-close-test-5",
        decision="no",
        actor_user_id=11,
        prompted_date="2026-02-23",
    )
    assert acknowledged is True
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is False
    assert state.get("prompted") is True
    assert state.get("decision") == "no"
    assert state.get("prompted_by") == "11"
    assert get_notice_send_prompt_candidate([di]) is None


def test_notice_send_ack_infers_candidate_from_existing_sent_history(app, db_session):
    from app.models.docket import DocketItem
    from app.models.email_automation import EmailMessage, EmailMessageMatterLink
    from app.models.ip_records import Communication
    from app.services.deadlines.notice_send_semi_close import ack_notice_send_prompt

    di = DocketItem(
        docket_id="semi-close-test-5b",
        matter_id="matter-semi-close-5b",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:refund-5b",
        name_free="Text Text(3Text Text) · Text Text Text",
        due_date="2026-03-10",
    )
    db_session.add(di)

    comm = Communication(
        comm_id="semi-close-test-5b-comm",
        matter_id="matter-semi-close-5b",
        comm_type="M",
        sent_date="2026-02-23",
        note="Text Text Text Text Text",
    )
    db_session.add(comm)
    email = EmailMessage(
        id="semi-close-test-5b-email",
        linked_comm_id="semi-close-test-5b-comm",
        body_text="Text Text Text. Text Text Text Text.",
        mailbox_tag="DOCKET",
    )
    db_session.add(email)
    db_session.add(
        EmailMessageMatterLink(
            email_id="semi-close-test-5b-email",
            matter_id="matter-semi-close-5b",
            comm_id="semi-close-test-5b-comm",
        )
    )
    db_session.commit()

    acknowledged = ack_notice_send_prompt(
        matter_id="matter-semi-close-5b",
        docket_id="semi-close-test-5b",
        decision="no",
        actor_user_id=12,
        prompted_date="2026-02-23",
    )
    assert acknowledged is True
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("prompted") is True
    assert state.get("trigger_doc_name") == "Text Text Text Text Text"


def test_notice_send_generic_task_allows_strict_broad_match(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import mark_notice_send_candidates

    di = DocketItem(
        docket_id="semi-close-test-6",
        matter_id="matter-semi-close-6",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-6",
        name_free="Text Text(3Text Text) · Text Text Text Text Text",
        due_date="2026-03-11",
        memo=json.dumps(
            {
                "auto": True,
                "received_date": "2026-02-23",
            },
            ensure_ascii=False,
        ),
    )
    db_session.add(di)
    db_session.commit()

    marked = mark_notice_send_candidates(
        matter_id="matter-semi-close-6",
        direction="Send",
        doc_name="Text Text Text Text Text",
        sent_date="2026-02-23",
        comm_type="M",
        source="unit_test_generic_hint",
    )
    assert marked == 1
    db_session.commit()
    db_session.refresh(di)
    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True


def test_notice_send_infer_ignores_outbound_before_trigger_received_date(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        infer_notice_send_prompt_candidate_from_communications,
    )

    di = DocketItem(
        docket_id="semi-close-test-7",
        matter_id="matter-semi-close-7",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-7",
        name_free="Text Text(3Text Text) · Text Text Text Text Text",
        due_date="2026-03-12",
        memo=json.dumps(
            {
                "auto": True,
                "received_date": "2026-02-23",
            },
            ensure_ascii=False,
        ),
    )
    db_session.add(di)
    db_session.commit()

    inferred = infer_notice_send_prompt_candidate_from_communications(
        docket_items=[di],
        communications=[
            {
                "doc_name": "Text Text",
                "comm_type": "M",
                "sent_date": "2026-02-12",  # before received_date -> must be ignored
            },
            {
                "doc_name": "Text Text",
                "comm_type": "M",
                "sent_date": "2026-02-23",
            },
        ],
    )
    assert inferred is not None
    assert inferred.get("docket_id") == "semi-close-test-7"


def test_notice_send_infer_prefers_latest_mail_and_ignores_telephone(app, db_session):
    from app.models.docket import DocketItem
    from app.services.deadlines.notice_send_semi_close import (
        infer_notice_send_prompt_candidate_from_communications,
    )

    di = DocketItem(
        docket_id="semi-close-test-8",
        matter_id="matter-semi-close-8",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-8",
        name_free="Text Text(3Text Text) · Text",
        due_date="2026-03-13",
    )
    db_session.add(di)
    db_session.commit()

    inferred = infer_notice_send_prompt_candidate_from_communications(
        docket_items=[di],
        communications=[
            {
                "doc_name": "Text Text",
                "comm_type": "M",
                "sent_date": "2026-02-23",
            },
            {
                "doc_name": "Text Text Text",
                "comm_type": "T",
                "sent_date": "2026-02-25",
            },
            {
                "doc_name": "Text Text",
                "comm_type": "M",
                "sent_date": "2026-02-24",
            },
        ],
    )
    assert inferred is not None
    assert inferred.get("docket_id") == "semi-close-test-8"
    assert inferred.get("matched_doc_name") == "Text Text"


def test_communication_service_create_marks_notice_send_candidate(app, db_session):
    from app.models.docket import DocketItem
    from app.services.history.communication_service import (
        CommunicationData,
        get_communication_service,
    )

    di = DocketItem(
        docket_id="semi-close-test-9",
        matter_id="matter-semi-close-9",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-9",
        name_free="Text Text(3Text Text) · Text",
        due_date="2026-03-14",
    )
    db_session.add(di)
    db_session.commit()

    result = get_communication_service().create(
        CommunicationData(
            matter_id="matter-semi-close-9",
            direction="Send",
            subject="Text Text",
            sent_date="2026-02-24",
            comm_type="M",
            source="unit_test_service_create",
            actor_user_id=31,
        )
    )
    assert result.success is True
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("prompted") is False
    assert state.get("trigger_doc_name") == "Text Text"
    assert state.get("candidate_by") == "31"


def test_communication_service_update_clears_stale_notice_send_candidate(app, db_session):
    from app.models.docket import DocketItem
    from app.services.history.communication_service import (
        CommunicationData,
        get_communication_service,
    )

    service = get_communication_service()
    di = DocketItem(
        docket_id="semi-close-test-10",
        matter_id="matter-semi-close-10",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-10",
        name_free="Text Text(3Text Text) · Text",
        due_date="2026-03-15",
    )
    db_session.add(di)
    db_session.commit()

    created = service.create(
        CommunicationData(
            matter_id="matter-semi-close-10",
            direction="Send",
            subject="Text Text",
            sent_date="2026-02-24",
            comm_type="M",
            source="unit_test_service_update_create",
            actor_user_id=32,
        )
    )
    assert created.success is True
    db_session.commit()

    updated = service.update(
        str(created.comm_id),
        CommunicationData(
            matter_id="matter-semi-close-10",
            direction="",
            subject="Text Text",
            received_date="2026-02-24",
            comm_type="M",
            source="unit_test_service_update_clear",
            actor_user_id=33,
        ),
    )
    assert updated.success is True
    db_session.commit()
    db_session.refresh(di)

    assert _state_from_memo(di.memo) == {}


def test_communication_service_delete_recomputes_notice_send_candidate(app, db_session):
    from app.models.docket import DocketItem
    from app.services.history.communication_service import (
        CommunicationData,
        get_communication_service,
    )

    service = get_communication_service()
    di = DocketItem(
        docket_id="semi-close-test-11",
        matter_id="matter-semi-close-11",
        category="NOTICE",
        name_ref="MGMT:NOTICE_SEND_3D:oa-11",
        name_free="Text Text(3Text Text) · Text",
        due_date="2026-03-16",
    )
    db_session.add(di)
    db_session.commit()

    first = service.create(
        CommunicationData(
            matter_id="matter-semi-close-11",
            direction="Send",
            subject="Text Text",
            sent_date="2026-02-23",
            comm_type="M",
            source="unit_test_service_delete_first",
        )
    )
    second = service.create(
        CommunicationData(
            matter_id="matter-semi-close-11",
            direction="Send",
            subject="Text Text",
            sent_date="2026-02-24",
            comm_type="M",
            source="unit_test_service_delete_second",
        )
    )
    assert first.success is True
    assert second.success is True
    db_session.commit()
    db_session.refresh(di)
    assert _state_from_memo(di.memo).get("trigger_doc_name") == "Text Text"

    deleted = service.delete(str(second.comm_id), "matter-semi-close-11")
    assert deleted.success is True
    db_session.commit()
    db_session.refresh(di)

    state = _state_from_memo(di.memo)
    assert state.get("candidate") is True
    assert state.get("trigger_doc_name") == "Text Text"
