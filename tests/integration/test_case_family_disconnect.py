from __future__ import annotations

import uuid


def test_family_disconnect_removes_manual_link_and_stores_auto_exclusion(
    app, db_session, admin_client
):
    from app.models.ip_records import Family, Matter, MatterCustomField, MatterFamily

    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="26PO0102US",
        right_name="Text-Text Text Text Text Text Text",
        right_group="OUT",
        matter_type="PATENT",
    )
    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="25PO0102US",
        right_name="Text Text Text Text Text",
        right_group="OUT",
        matter_type="PATENT",
    )
    fam = Family(family_id=uuid.uuid4().hex, family_key="FAM-TEST-0102")

    db_session.add_all([source, target, fam])
    db_session.flush()
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(source.matter_id),
            family_id=str(fam.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(target.matter_id),
            family_id=str(fam.family_id),
            link_role="manual",
        )
    )
    db_session.commit()

    resp = admin_client.post(
        f"/case/{source.matter_id}/family/disconnect",
        data={"target_matter_id": str(target.matter_id)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert f"/case/{source.matter_id}#sec-family" in (resp.headers.get("Location") or "")

    # Family with only two members should be cleaned up after disconnect.
    assert Family.query.filter_by(family_id=str(fam.family_id)).first() is None
    assert MatterFamily.query.filter(MatterFamily.family_id == str(fam.family_id)).count() == 0

    source_pref = MatterCustomField.query.filter_by(
        matter_id=str(source.matter_id), namespace="family"
    ).first()
    target_pref = MatterCustomField.query.filter_by(
        matter_id=str(target.matter_id), namespace="family"
    ).first()
    assert source_pref is not None and isinstance(source_pref.data, dict)
    assert target_pref is not None and isinstance(target_pref.data, dict)
    assert str(target.matter_id) in (source_pref.data.get("excluded_related_matter_ids") or [])
    assert str(source.matter_id) in (target_pref.data.get("excluded_related_matter_ids") or [])


def test_family_disconnect_handles_bridged_family_component(app, db_session, admin_client):
    from app.models.ip_records import Family, Matter, MatterCustomField, MatterFamily

    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="26PD0104US",
        right_name="Text-Text Text Text Text Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    bridge = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="25PD0104US",
        right_name="Text Text Text Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="24PD0104US",
        right_name="Text Text Text Text Text Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    fam1 = Family(family_id=uuid.uuid4().hex, family_key="FAM-BRIDGE-1")
    fam2 = Family(family_id=uuid.uuid4().hex, family_key="FAM-BRIDGE-2")

    db_session.add_all([source, bridge, target, fam1, fam2])
    db_session.flush()
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(source.matter_id),
            family_id=str(fam1.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(bridge.matter_id),
            family_id=str(fam1.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(bridge.matter_id),
            family_id=str(fam2.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(target.matter_id),
            family_id=str(fam2.family_id),
            link_role="manual",
        )
    )
    db_session.commit()

    resp = admin_client.post(
        f"/case/{source.matter_id}/family/disconnect",
        data={"target_matter_id": str(target.matter_id)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert f"/case/{source.matter_id}#sec-family" in (resp.headers.get("Location") or "")

    # target should be disconnected from the connected component reachable via bridge
    assert MatterFamily.query.filter_by(matter_id=str(target.matter_id)).count() == 0

    # degenerate bridged family should be cleaned up (bridge alone in fam2 after target removal)
    assert Family.query.filter_by(family_id=str(fam2.family_id)).first() is None
    assert (
        MatterFamily.query.filter_by(
            matter_id=str(bridge.matter_id), family_id=str(fam2.family_id)
        ).count()
        == 0
    )

    # source family remains intact
    assert Family.query.filter_by(family_id=str(fam1.family_id)).first() is not None
    assert (
        MatterFamily.query.filter_by(
            matter_id=str(source.matter_id), family_id=str(fam1.family_id)
        ).count()
        == 1
    )
    assert (
        MatterFamily.query.filter_by(
            matter_id=str(bridge.matter_id), family_id=str(fam1.family_id)
        ).count()
        == 1
    )

    source_pref = MatterCustomField.query.filter_by(
        matter_id=str(source.matter_id), namespace="family"
    ).first()
    target_pref = MatterCustomField.query.filter_by(
        matter_id=str(target.matter_id), namespace="family"
    ).first()
    assert source_pref is not None and isinstance(source_pref.data, dict)
    assert target_pref is not None and isinstance(target_pref.data, dict)
    assert str(target.matter_id) in (source_pref.data.get("excluded_related_matter_ids") or [])
    assert str(source.matter_id) in (target_pref.data.get("excluded_related_matter_ids") or [])
