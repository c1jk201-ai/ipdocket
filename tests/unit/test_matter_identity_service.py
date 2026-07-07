from __future__ import annotations

import uuid


def test_matter_identity_service_resolves_canonical_and_alias_refs(app, db_session):
    from app.models.ip_records import Matter
    from app.services.matter.matter_identity_service import MatterIdentityService

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="26PD0100US",
        old_our_ref="OLD-26PD0100",
        your_ref="CLIENT-REF-1",
    )
    db_session.add(matter)
    db_session.commit()

    assert MatterIdentityService.resolve_matter_id_for_case_ref("26PD0100US") == matter.matter_id
    assert MatterIdentityService.resolve_matter_id_for_case_ref("OLD-26PD0100") == matter.matter_id
    assert MatterIdentityService.resolve_matter_id_for_case_ref("CLIENT-REF-1") == matter.matter_id


def test_matter_identity_service_normalized_match_is_unique_only(app, db_session):
    from app.models.ip_records import Matter
    from app.services.matter.matter_identity_service import MatterIdentityService

    matter = Matter(matter_id=uuid.uuid4().hex, our_ref="26-PD-0101-US")
    db_session.add(matter)
    db_session.commit()

    found = MatterIdentityService.find_by_reference("26PD0101US", allow_normalized=True)

    assert found is not None
    assert found.matter_id == matter.matter_id


def test_matter_identity_service_matches_our_ref_zero_padding_variant(app, db_session):
    from app.models.ip_records import Matter
    from app.services.matter.matter_identity_service import MatterIdentityService

    matter = Matter(matter_id=uuid.uuid4().hex, our_ref="26PD0168US")
    db_session.add(matter)
    db_session.commit()

    found = MatterIdentityService.find_by_reference("26PD168US", allow_normalized=True)

    assert found is not None
    assert found.matter_id == matter.matter_id


def test_auto_match_scores_our_ref_zero_padding_variant(app, db_session):
    from app.models.ip_records import Matter
    from app.services.matching.auto_match_service import score_matter_candidates

    matter = Matter(matter_id=uuid.uuid4().hex, our_ref="26PD0168US")
    db_session.add(matter)
    db_session.commit()

    candidates = score_matter_candidates({"our_ref": "26PD168US"})

    assert candidates
    assert candidates[0]["matter_id"] == matter.matter_id
    assert candidates[0]["score"] == 100


def test_matter_identity_service_ignores_deleted_matters(app, db_session):
    from app.models.ip_records import Matter
    from app.services.matter.matter_identity_service import MatterIdentityService

    matter = Matter(matter_id=uuid.uuid4().hex, our_ref="26PD0102US", is_deleted=True)
    db_session.add(matter)
    db_session.commit()

    assert MatterIdentityService.resolve_matter_id_for_case_ref("26PD0102US") is None
