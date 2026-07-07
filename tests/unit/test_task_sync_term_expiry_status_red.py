from __future__ import annotations

from datetime import date, timedelta


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_should_skip_workflow_for_renewal_managed_term_expiry_docket(db_session, sample_matter):
    from app.models.docket import DocketItem
    from app.services.workflow.task_sync import _should_skip_workflow_for_docket

    matter = db_session.merge(sample_matter)
    matter.our_ref = "26TD0001US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    matter.status_red = "Term expired"
    matter.status_red_related_date = "2035-03-13"
    db_session.add(matter)
    db_session.flush()

    docket = DocketItem(
        matter_id=_matter_id(sample_matter),
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Term expired",
        name_free="Term expired",
        due_date=(date.today() + timedelta(days=30)).isoformat(),
        done_date=None,
        is_deleted=False,
    )
    db_session.add(docket)
    db_session.flush()

    assert _should_skip_workflow_for_docket(docket) is True


def test_sync_from_docket_item_skipped_term_expiry_cleans_completed_workflow(
    app, db_session, sample_matter, monkeypatch
):
    import uuid

    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow import task_sync as task_sync_mod

    matter = db_session.merge(sample_matter)
    matter.our_ref = "26TD0002US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    matter.status_red = "Term expired"
    matter.status_red_related_date = "2035-03-13"
    db_session.add(matter)
    db_session.flush()

    docket_id = f"term-expiry-skip-{uuid.uuid4().hex[:8]}"
    docket = DocketItem(
        docket_id=docket_id,
        matter_id=_matter_id(sample_matter),
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Term expired",
        name_free="Term expired",
        due_date="2035-03-13",
        done_date=date.today().isoformat(),
        is_deleted=False,
    )
    workflow = Workflow(
        case_id=_matter_id(sample_matter),
        name="Term expired",
        status="Completed",
        due_date=date(2035, 3, 13),
        completed_date=date.today(),
        business_code=f"DOCKET:{docket_id}",
        note=" Create: DocketItem ",
    )
    db_session.add_all([docket, workflow])
    db_session.commit()

    deleted_workflow_ids: list[int] = []
    cleanup_calls: list[tuple[str, bool]] = []
    cleanup_results: list[set[int]] = []
    real_cleanup = task_sync_mod._cleanup_skipped_docket_workflows

    def _record_cleanup(docket_item, *, delete_auto_generated=False):
        cleanup_calls.append((docket_item.docket_id, delete_auto_generated))
        result = real_cleanup(docket_item, delete_auto_generated=delete_auto_generated)
        cleanup_results.append(result)
        return result

    monkeypatch.setattr(task_sync_mod, "_cleanup_skipped_docket_workflows", _record_cleanup)
    monkeypatch.setattr(
        task_sync_mod,
        "_delete_workflow_for_distribution_cleanup",
        lambda *, workflow_id: deleted_workflow_ids.append(workflow_id),
    )
    assert task_sync_mod._is_renewal_managed_term_expiry_docket(docket) is True
    assert task_sync_mod._should_delete_skipped_docket_workflows(docket) is True

    task_sync_mod.sync_from_docket_item(docket_item=docket, actor_id=None)
    db_session.commit()

    assert cleanup_calls == [(docket_id, True)]
    assert cleanup_results == [{workflow.id}]
    assert deleted_workflow_ids == [workflow.id]
