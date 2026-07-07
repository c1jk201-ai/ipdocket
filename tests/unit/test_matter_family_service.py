from __future__ import annotations

import uuid


def _add_matter(db_session, *, our_ref: str):
    from app.models.ip_records import Matter

    m = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=our_ref,
        right_name=our_ref,
        right_group="OUT",
        matter_type="PATENT",
    )
    db_session.add(m)
    return m


def test_link_matters_merges_connected_families_into_single_family_id(app, db_session):
    from app.models.ip_records import Family, MatterFamily
    from app.services.matter.matter_family_service import link_matters_into_family

    m_a = _add_matter(db_session, our_ref="26PO0102US")
    m_b = _add_matter(db_session, our_ref="25PO0102US")
    m_c = _add_matter(db_session, our_ref="24PO0102US")
    m_d = _add_matter(db_session, our_ref="23PO0102US")

    fam1 = Family(family_id=uuid.uuid4().hex, family_key="FAM-PO-1")
    fam2 = Family(family_id=uuid.uuid4().hex, family_key="FAM-PO-2")
    db_session.add_all([fam1, fam2])
    db_session.flush()

    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(m_a.matter_id),
            family_id=str(fam1.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(m_b.matter_id),
            family_id=str(fam1.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(m_b.matter_id),
            family_id=str(fam2.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(m_c.matter_id),
            family_id=str(fam2.family_id),
            link_role="manual",
        )
    )
    db_session.add(
        MatterFamily(
            mf_id=uuid.uuid4().hex,
            matter_id=str(m_d.matter_id),
            family_id=str(fam2.family_id),
            link_role="manual",
        )
    )
    db_session.commit()

    family_id, family_key, _created = link_matters_into_family(
        primary_matter=m_a, target_matter=m_c, prefer_primary=True, link_role="manual"
    )
    db_session.commit()

    assert family_id == str(fam1.family_id)
    assert family_key == "FAM-PO-1"

    assert Family.query.filter_by(family_id=str(fam2.family_id)).first() is None

    for m in (m_a, m_b, m_c, m_d):
        rows = MatterFamily.query.filter_by(matter_id=str(m.matter_id)).all()
        assert len(rows) == 1
        assert (rows[0].family_id or "").strip() == str(fam1.family_id)
