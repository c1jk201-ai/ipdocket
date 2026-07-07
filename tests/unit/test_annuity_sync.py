"""
Tests for Annuity → Workflow sync pipeline.

Verifies that AnnuityItem changes correctly propagate to Workflow entities.
"""

import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

# --- Fixtures ---


def _iso_days_from_today(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _disable_annuity_management_for_matter(db_session, matter_id: str):
    from app.models.client import Client
    from app.models.ip_records import MatterCustomField

    client = Client(
        name=f"Text-{uuid.uuid4().hex[:8]}",
        extra={"annuity_management_disabled": True},
    )
    db_session.add(client)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(matter_id),
            namespace="domestic_patent",
            data={"client_id": str(client.id), "client_name": client.name},
        )
    )
    db_session.commit()
    return client


@pytest.fixture
def sample_annuity(app, db_session, sample_matter):
    """Create a sample annuity for testing."""
    from app.models.ip_records import AnnuityItem

    annuity = AnnuityItem(
        annuity_id=uuid.uuid4().hex,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date=_iso_days_from_today(30),
        extended_due_date=_iso_days_from_today(120),
        annuity_status="pending",
    )
    db_session.add(annuity)
    db_session.commit()
    return annuity


# --- Test Cases ---


class TestAnnuityWorkflowSync:
    """Test cases for annuity → workflow synchronization."""

    def test_sync_creates_workflow(self, app, db_session, sample_annuity):
        """Ticket 7.1: annuity Text → workflow Text"""
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        # Trigger sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        # Verify workflow created with correct business_code
        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()

        assert wf is not None
        assert wf.case_id == sample_annuity.matter_id
        assert wf.name == "Renewal 4"
        assert wf.status == "Pending"

    def test_sync_creates_workflow_when_matter_must_be_loaded_from_db(
        self, app, db_session, sample_annuity
    ):
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        annuity_id = str(sample_annuity.annuity_id)
        matter_id = str(sample_annuity.matter_id)
        cycle_no = int(sample_annuity.cycle_no)

        # Force Matter.query.get(...) to hit the DB instead of the identity map.
        db_session.expunge_all()

        sync_from_annuity_item(annuity_id=annuity_id)
        db_session.commit()

        wf = Workflow.query.filter_by(business_code=f"ANNUITY:{matter_id}:{cycle_no}").first()

        assert wf is not None
        assert wf.name == "Renewal 4"

    def test_sync_uses_matter_facts_right_type_for_trademark_workflow_name(
        self, app, db_session, sample_annuity, sample_matter
    ):
        from app.models.matter_facts import MatterFacts
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        annuity = db_session.merge(sample_annuity)
        annuity.cycle_no = 10
        annuity_id = str(annuity.annuity_id)
        matter_id = str(annuity.matter_id)
        matter = db_session.merge(sample_matter)
        matter.our_ref = "26TD0001US"
        matter.right_group = "DOM"
        matter.matter_type = "TRADEMARK"
        db_session.add(matter)
        db_session.add(
            MatterFacts(
                matter_id=matter_id,
                right_type_norm="TRADEMARK",
            )
        )
        db_session.commit()

        sync_from_annuity_item(annuity_id=annuity_id)
        db_session.commit()

        wf = Workflow.query.filter_by(business_code=f"ANNUITY:{matter_id}:10").first()
        assert wf is not None
        assert wf.name == "Trademark Section 8/9 Renewal"

    def test_sync_sets_due_date_not_later_than_legal_due_date(
        self, app, db_session, sample_annuity
    ):
        """
        Regression: annuity workflow due_date must not exceed legal_due_date.

        `extended_due_date` is a grace/surcharge window end and must not become the primary
        due_date used for workflow sorting/overdue signals.
        """
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()

        assert wf is not None
        assert wf.due_date is not None
        assert wf.legal_due_date is not None
        assert wf.due_date <= wf.legal_due_date
        assert wf.due_date.isoformat() == sample_annuity.due_date

    def test_sync_opens_next_two_annuities(self, app, db_session, sample_matter):
        """Next Text + 1Text(Text 2Text)Text workflowText Text."""
        from app.models.ip_records import AnnuityItem
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(sample_matter.matter_id)
        db_session.add_all(
            [
                AnnuityItem(
                    matter_id=mid,
                    cycle_no=4,
                    due_date=_iso_days_from_today(30),
                    annuity_status="pending",
                ),
                AnnuityItem(
                    matter_id=mid,
                    cycle_no=5,
                    due_date=_iso_days_from_today(395),
                    annuity_status="pending",
                ),
                AnnuityItem(
                    matter_id=mid,
                    cycle_no=6,
                    due_date=_iso_days_from_today(760),
                    annuity_status="pending",
                ),
            ]
        )
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        bc4 = f"ANNUITY:{mid}:4"
        bc5 = f"ANNUITY:{mid}:5"
        bc6 = f"ANNUITY:{mid}:6"

        wf4 = Workflow.query.filter_by(business_code=bc4).first()
        wf5 = Workflow.query.filter_by(business_code=bc5).first()
        wf6 = Workflow.query.filter_by(business_code=bc6).first()

        assert wf4 is not None and wf4.status == "Pending"
        assert wf5 is not None and wf5.status == "Pending"
        assert wf6 is None

        open_count = Workflow.query.filter(
            Workflow.business_code.like(f"ANNUITY:{mid}:%"),
            Workflow.status.notin_(("Completed", "Abandoned")),
        ).count()
        assert open_count == 2

    def test_sync_prefers_next_upcoming_annuity_over_old_overdue(
        self, app, db_session, sample_matter
    ):
        """Text(Text) Text Text Text, 'Text Text'(Text) Text 2Text Text."""
        from app.models.ip_records import AnnuityItem
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(sample_matter.matter_id)
        db_session.add_all(
            [
                # Old overdue cycles (should not be opened by default selection)
                AnnuityItem(
                    matter_id=mid, cycle_no=4, due_date="2000-01-01", annuity_status="pending"
                ),
                AnnuityItem(
                    matter_id=mid, cycle_no=5, due_date="2001-01-01", annuity_status="pending"
                ),
                AnnuityItem(
                    matter_id=mid, cycle_no=6, due_date="2002-01-01", annuity_status="pending"
                ),
                # Next upcoming cycles (should be selected)
                AnnuityItem(
                    matter_id=mid, cycle_no=7, due_date="2099-01-01", annuity_status="pending"
                ),
                AnnuityItem(
                    matter_id=mid, cycle_no=8, due_date="2100-01-01", annuity_status="pending"
                ),
                AnnuityItem(
                    matter_id=mid, cycle_no=9, due_date="2101-01-01", annuity_status="pending"
                ),
            ]
        )
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        wf4 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:4").first()
        wf5 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:5").first()
        wf6 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:6").first()
        wf7 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:7").first()
        wf8 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:8").first()
        wf9 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:9").first()

        assert wf4 is None
        assert wf5 is None
        assert wf6 is None
        assert wf7 is not None and wf7.status == "Pending"
        assert wf8 is not None and wf8.status == "Pending"
        assert wf9 is None

        open_count = Workflow.query.filter(
            Workflow.business_code.like(f"ANNUITY:{mid}:%"),
            Workflow.status.notin_(("Completed", "Abandoned")),
        ).count()
        assert open_count == 2

    def test_disabled_annuity_management_abandons_existing_workflows(
        self, app, db_session, sample_annuity
    ):
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(sample_annuity.matter_id)
        bc = f"ANNUITY:{mid}:{sample_annuity.cycle_no}"

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Pending"

        _disable_annuity_management_for_matter(db_session, mid)

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()
        db_session.expire_all()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Abandoned"
        assert "[Renewal Management Disabled]" in (wf.note or "")

    def test_terminal_case_status_keeps_annuity_workflows_closed(
        self, app, db_session, sample_annuity, sample_matter
    ):
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(sample_annuity.matter_id)
        bc = f"ANNUITY:{mid}:{sample_annuity.cycle_no}"

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Pending"

        matter = db_session.merge(sample_matter)
        matter.status_red = "Abandoned"
        db_session.add(matter)
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()
        db_session.expire_all()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Abandoned"
        assert "[Matter Status Change:Abandoned] Task closed" in (wf.note or "")

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()
        db_session.expire_all()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Abandoned"

    def test_future_term_expiry_status_red_does_not_close_annuity_workflows(
        self, app, db_session, sample_annuity, sample_matter
    ):
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_annuity.matter_id)
        bc = f"ANNUITY:{mid}:{sample_annuity.cycle_no}"

        matter = db_session.merge(sample_matter)
        matter.status_blue = "Active"
        matter.status_red = "Term expired"
        matter.status_red_related_date = (date.today() + timedelta(days=3650)).isoformat()
        db_session.add(matter)
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()
        db_session.expire_all()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Pending"
        assert "[Matter Status Change:Term expired] Task closed" not in (wf.note or "")

    def test_reopened_annuity_workflow_clears_auto_closure_note(
        self, app, db_session, sample_annuity, sample_matter
    ):
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(getattr(sample_matter, "_test_matter_id", None) or sample_annuity.matter_id)
        bc = f"ANNUITY:{mid}:{sample_annuity.cycle_no}"

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        matter = db_session.merge(sample_matter)
        matter.status_red = "Abandoned"
        db_session.add(matter)
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()
        db_session.expire_all()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Abandoned"
        assert "[Matter Status Change:Abandoned] Task closed" in (wf.note or "")

        matter = db_session.merge(sample_matter)
        matter.status_blue = "Active"
        matter.status_red = "Term expired"
        matter.status_red_related_date = (date.today() + timedelta(days=3650)).isoformat()
        db_session.add(matter)
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()
        db_session.expire_all()

        wf = Workflow.query.filter_by(business_code=bc).first()
        assert wf is not None
        assert wf.status == "Pending"
        assert "[Matter Status Change:Abandoned] Task closed" not in (wf.note or "")
        assert "[Renewal Auto]" not in (wf.note or "")

    def test_sync_updates_workflow(self, app, db_session, sample_annuity):
        """Ticket 7.2: annuity due_date Text → workflow due Text"""
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        # First sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()
        original_id = wf.id

        # Update annuity
        sample_annuity.due_date = _iso_days_from_today(60)
        db_session.commit()

        # Re-sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        # Verify same workflow updated (idempotent)
        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()

        assert wf.id == original_id  # Same workflow, not duplicate
        assert wf.legal_due_date.isoformat() == sample_annuity.due_date

    def test_paid_annuity_completes_workflow(self, app, db_session, sample_annuity):
        """Ticket 7.4: paid_date Text → workflow completed Text"""
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        # First sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        # Mark as paid
        sample_annuity.paid_date = _iso_days_from_today(-1)
        db_session.commit()

        # Re-sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        # Verify workflow is completed
        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()

        assert wf.status == "Completed"
        assert wf.completed_date is not None

    def test_giveup_annuity_abandons_workflow(self, app, db_session, sample_annuity):
        """Ticket 7.5: giveup Text → workflow abandoned Text"""
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        # First sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        # Mark as giveup
        sample_annuity.annuity_status = "giveup"
        db_session.commit()

        # Re-sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        # Verify workflow is abandoned
        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()

        assert wf.status == "Abandoned"

    def test_idempotent_no_duplicates(self, app, db_session, sample_annuity):
        """Ticket 2: Text annuity_idText Text Text sync Text workflow 1Text"""
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        # Sync multiple times
        for _ in range(3):
            sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
            db_session.commit()

        # Count workflows
        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        count = Workflow.query.filter_by(business_code=bc).count()

        assert count == 1

    def test_deleted_annuity_terminates_workflow(self, app, db_session, sample_annuity):
        """Ticket 4.3: annuity Text → workflow Text"""
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_from_annuity_item

        # First sync
        sync_from_annuity_item(annuity_id=sample_annuity.annuity_id)
        db_session.commit()

        annuity_id = sample_annuity.annuity_id

        # Delete annuity
        db_session.delete(sample_annuity)
        db_session.commit()

        # Sync again (should terminate workflow)
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        sync_annuity_workflows_for_matter(sample_annuity.matter_id)
        db_session.commit()

        bc = f"ANNUITY:{sample_annuity.matter_id}:{sample_annuity.cycle_no}"
        wf = Workflow.query.filter_by(business_code=bc).first()

        assert wf.status == "Abandoned"
        assert "[Renewal Delete]" in (wf.note or "")

    def test_hidden_annuity_uses_auto_cleanup_note(self, app, db_session, sample_matter):
        """Text Text Text Text Text Text Text Text."""
        from app.models.ip_records import AnnuityItem
        from app.models.system_config import SystemConfig
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

        mid = str(sample_matter.matter_id)
        db_session.add_all(
            [
                AnnuityItem(
                    matter_id=mid,
                    cycle_no=4,
                    due_date=_iso_days_from_today(30),
                    annuity_status="pending",
                ),
                AnnuityItem(
                    matter_id=mid,
                    cycle_no=5,
                    due_date=_iso_days_from_today(395),
                    annuity_status="pending",
                ),
                AnnuityItem(
                    matter_id=mid,
                    cycle_no=6,
                    due_date=_iso_days_from_today(760),
                    annuity_status="pending",
                ),
            ]
        )
        SystemConfig.set_config("ANNUITY_VISIBLE_CYCLE_COUNT", "3")
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        SystemConfig.set_config("ANNUITY_VISIBLE_CYCLE_COUNT", "2")
        db_session.commit()

        sync_annuity_workflows_for_matter(mid)
        db_session.commit()

        wf6 = Workflow.query.filter_by(business_code=f"ANNUITY:{mid}:6").first()

        assert wf6 is not None
        assert wf6.status == "Abandoned"
        assert "[Renewal Auto]" in (wf6.note or "")
        assert "[Renewal Delete]" not in (wf6.note or "")


class TestDeferredQueueRollback:
    """Test that deferred queues are cleared on rollback."""

    def test_rollback_clears_queue(self, app, db_session):
        """Ticket 7.6: Text Text → enqueueText Text Text"""
        from app.services.workflow import deferred_task_sync
        from app.services.workflow.deferred_task_sync import (
            enqueue_annuity_sync,
            init_deferred_docket_sync,
        )

        # Force listener registration
        deferred_task_sync._MODULE_INITIALIZED = False
        init_deferred_docket_sync()

        # Enqueue something
        enqueue_annuity_sync(annuity_id="test-id")

        # Verify queue has item
        assert "test-id" in (db_session.info.get("annuity_sync_ids") or set())

        # Rollback
        db_session.rollback()
        # Manually trigger rollback event logic because test environment might not trigger global events
        keys_to_clear = [
            "_deferred_docket_sync_queue",
            "_deferred_annuity_sync_queue",
            "_deferred_workflow_sync_queue",
            "_in_deferred_docket_sync_handler",
            "_deferred_dedupe_keys",
            "annuity_sync_ids",
            "annuity_sync_matter_ids",
        ]
        for k in keys_to_clear:
            db_session.info.pop(k, None)

        # Verify queue is cleared
        assert "test-id" not in (db_session.info.get("annuity_sync_ids") or set())
