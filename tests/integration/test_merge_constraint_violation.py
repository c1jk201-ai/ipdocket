import psycopg2
import pytest
from sqlalchemy.exc import IntegrityError

from app.models.client import Client
from app.services.client.client_merge_service import ClientMergeService


def test_client_merge_unique_violation_repro(db_session, monkeypatch):
    """
    Reproduction test for UniqueViolation during client merge.
    Scenario:
    - Client A (source) has ipm_client_id=27
    - Client B (target) has ipm_client_id=None
    - Merge A into B.
    - Expected: B gets ipm_client_id=27, A loses it (or is deleted).
    - Current Bug: Setting B.ipm_client_id=27 before clearing A causes UniqueViolation if flushed.
    """

    # 1. Setup
    client_source = Client(
        name="Source Client", ipm_client_id=27, party_id="src_party_id", email="source@example.com"
    )
    client_target = Client(
        name="Target Client",
        ipm_client_id=None,
        party_id="tgt_party_id",
        email="target@example.com",
    )
    db_session.add(client_source)
    db_session.add(client_target)
    db_session.commit()

    source_id = client_source.id
    target_id = client_target.id

    monkeypatch.setattr(
        ClientMergeService,
        "_create_premerge_backup",
        lambda **_: {"path": "test-premerge-backup", "kind": "test"},
    )

    # 2. Execute Merge
    # Depending on the exact timing of autoflush in the service, this might raise IntegrityError
    try:
        result = ClientMergeService.merge_clients(
            target_client_id=target_id, source_client_ids=[source_id], reason="Repro merge"
        )
        assert result["ok"] is True
    except (IntegrityError, psycopg2.errors.UniqueViolation) as e:
        pytest.fail(f"Caught expected UniqueViolation: {e}")
    except Exception as e:
        pytest.fail(f"Caught unexpected exception: {e}")

    # 3. Verify
    db_session.expire_all()
    t = db_session.get(Client, target_id)
    s = db_session.get(Client, source_id)

    assert t.ipm_client_id == 27, "Target should adopt the unique ID"
    assert s.is_deleted is True, "Source should be deleted"
    # Note: Soft deleted source still holds the value in some implementations unless explicitly cleared.
    # In the fixed version, we expect it to be cleared to allow the Unique constraint on target.
    assert s.ipm_client_id is None or s.ipm_client_id != 27
