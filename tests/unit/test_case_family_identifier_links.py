from __future__ import annotations

import uuid


def _add_matter(db_session, *, our_ref: str, identifiers: list[tuple[str, str]]):
    from app.models.ip_records import Matter, MatterIdentifier, VMatterOverview

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=our_ref,
        right_name=f"{our_ref} title",
        right_group="OUT" if our_ref.endswith(("US", "CN")) else "DOM",
        matter_type="PCT" if our_ref.endswith("PCT") else "PATENT",
        status_blue="Text",
        is_deleted=False,
    )
    db_session.add(matter)
    db_session.flush()
    db_session.add(
        VMatterOverview(
            matter_id=matter.matter_id,
            our_ref=matter.our_ref,
            right_name=matter.right_name,
            right_group=matter.right_group,
            matter_type=matter.matter_type,
            status_blue=matter.status_blue,
        )
    )
    for id_type, id_value in identifiers:
        db_session.add(
            MatterIdentifier(
                matter_id=matter.matter_id,
                id_type=id_type,
                id_value=id_value,
            )
        )
    return matter


def _add_pct_national_phase_chain(db_session):
    priority_no = "10-2022-0144526"
    pct_no = "PCT/US2023/016318"
    matters = {
        "source": _add_matter(
            db_session,
            our_ref="23PD0137US",
            identifiers=[("Priority", priority_no)],
        ),
        "pct": _add_matter(
            db_session,
            our_ref="23PD0102PCT",
            identifiers=[
                ("PCT Application No.", pct_no),
                ("Priority", priority_no),
            ],
        ),
        "us": _add_matter(
            db_session,
            our_ref="24PO0101US",
            identifiers=[
                ("Application No.", "18/596,652"),
                ("Parent application No.", pct_no),
                ("Priority", priority_no),
                ("PCT Application No.", pct_no),
            ],
        ),
        "cn": _add_matter(
            db_session,
            our_ref="25PO0101CN",
            identifiers=[
                ("Application No.", "2023800621057"),
                ("Priority", priority_no),
            ],
        ),
    }
    db_session.commit()
    return matters


def test_family_section_groups_pct_and_national_phases_by_identifiers(db_session):
    from app.blueprints.case.services.detail_context import _build_base, _build_family_section

    matters = _add_pct_national_phase_chain(db_session)

    def related_refs(key: str) -> set[str]:
        matter = matters[key]
        ctx = _build_base(str(matter.matter_id), {}, None)
        ctx["_current_user"] = None
        family = _build_family_section(ctx)
        return {row["our_ref"] for row in family["related_family_rows"]}

    assert related_refs("pct") == {"23PD0137US", "24PO0101US", "25PO0101CN"}
    assert related_refs("cn") == {"23PD0137US", "23PD0102PCT", "24PO0101US"}
    assert related_refs("source") == {"23PD0102PCT", "24PO0101US", "25PO0101CN"}


def test_identifier_permission_fallback_uses_shared_priority_and_pct_reference(db_session):
    from app.models.ip_records import Matter
    from app.utils.permissions import _load_identifier_related_matter_ids

    matters = _add_pct_national_phase_chain(db_session)
    refs_by_id = {
        str(m.matter_id): m.our_ref
        for m in Matter.query.filter(Matter.matter_id.in_([m.matter_id for m in matters.values()]))
    }

    pct_related_refs = {
        refs_by_id[mid]
        for mid in _load_identifier_related_matter_ids(matter_id=str(matters["pct"].matter_id))
    }
    cn_related_refs = {
        refs_by_id[mid]
        for mid in _load_identifier_related_matter_ids(matter_id=str(matters["cn"].matter_id))
    }

    assert pct_related_refs == {"23PD0137US", "24PO0101US", "25PO0101CN"}
    assert cn_related_refs == {"23PD0137US", "23PD0102PCT", "24PO0101US"}
