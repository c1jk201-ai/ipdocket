from __future__ import annotations

from app.blueprints.case.services.detail_context import _build_family_section
from app.models.ip_records import (
    Family,
    Matter,
    MatterCustomField,
    MatterFamily,
    MatterIdentifier,
    VMatterOverview,
)


def _add_matter(
    db_session,
    *,
    matter_id: str,
    our_ref: str,
    right_group: str = "DOM",
    matter_type: str = "PATENT",
    right_name: str = "",
):
    title = right_name or our_ref
    matter = Matter(
        matter_id=matter_id,
        our_ref=our_ref,
        right_group=right_group,
        matter_type=matter_type,
        right_name=title,
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref=our_ref,
        right_group=right_group,
        matter_type=matter_type,
        right_name=title,
    )
    db_session.add(matter)
    db_session.add(overview)
    return matter, overview


def _make_ctx(
    *, mid: str, matter: Matter, overview: VMatterOverview, identifiers: dict[str, list[str]]
):
    return {
        "_mid_str": mid,
        "matter": matter,
        "overview": overview,
        "identifiers": identifiers,
    }


def test_family_section_excludes_self_from_explicit_family(app, db_session):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0001")
    _m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="26PD0002")

    db_session.add(Family(family_id="fam1", family_key="FAM-1"))
    db_session.add(MatterFamily(mf_id="mf1", matter_id="m1", family_id="fam1", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf2", matter_id="m2", family_id="fam1", link_role="manual"))
    db_session.commit()

    out = _build_family_section(_make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={}))

    rows = out.get("related_family_rows") or []
    assert out.get("family_count") == 1
    assert len(rows) == 1
    assert rows[0]["matter_id"] == "m2"


def test_family_section_excludes_soft_deleted_related_matter(app, db_session):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0001")
    m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="26PD0002")
    m2.is_deleted = True

    db_session.add(Family(family_id="fam1", family_key="FAM-1"))
    db_session.add(MatterFamily(mf_id="mf1", matter_id="m1", family_id="fam1", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf2", matter_id="m2", family_id="fam1", link_role="manual"))
    db_session.commit()

    out = _build_family_section(_make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={}))

    assert out.get("family_count") == 0
    assert (out.get("related_family_rows") or []) == []
    assert (out.get("family_keys") or []) == []


def test_family_section_count_respects_acl_filter(app, db_session, monkeypatch):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0001")
    _m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="26PD0002")

    db_session.add(Family(family_id="fam1", family_key="FAM-1"))
    db_session.add(MatterFamily(mf_id="mf1", matter_id="m1", family_id="fam1", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf2", matter_id="m2", family_id="fam1", link_role="manual"))
    db_session.commit()

    def _deny_related(_user, matter_id: str, action: str = "view") -> bool:
        return matter_id != "m2"

    monkeypatch.setattr("app.utils.permissions.can_access_matter", _deny_related)
    ctx = _make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={})
    ctx["_current_user"] = object()

    out = _build_family_section(ctx)

    assert out.get("family_count") == 0
    assert (out.get("related_family_rows") or []) == []


def test_family_section_includes_connected_component_across_bridged_family_ids(app, db_session):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0104US")
    _m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="25PD0104US")
    _m3, _ov3 = _add_matter(db_session, matter_id="m3", our_ref="24PD0104US")

    db_session.add(Family(family_id="fam1", family_key="FAM-1"))
    db_session.add(Family(family_id="fam2", family_key="FAM-2"))
    db_session.add(MatterFamily(mf_id="mf1", matter_id="m1", family_id="fam1", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf2", matter_id="m2", family_id="fam1", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf3", matter_id="m2", family_id="fam2", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf4", matter_id="m3", family_id="fam2", link_role="manual"))
    db_session.commit()

    out = _build_family_section(_make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={}))

    rows = out.get("related_family_rows") or []
    mids = {r.get("matter_id") for r in rows}
    assert out.get("family_count") == 2
    assert mids == {"m2", "m3"}
    assert set(out.get("family_keys") or []) == {"FAM-1", "FAM-2"}
    for row in rows:
        assert row.get("work_label") == "US - Patent (Family)"


def test_family_section_finds_all_normalized_identifier_matches(app, db_session):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0101")
    _m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="26PD0102")
    _m3, _ov3 = _add_matter(db_session, matter_id="m3", our_ref="26PD0103")

    db_session.add(
        MatterIdentifier(
            mid_id="id1",
            matter_id="m1",
            id_type="Priority",
            id_value="10-2024-1234567",
        )
    )
    db_session.add(
        MatterIdentifier(
            mid_id="id2",
            matter_id="m2",
            id_type="APP_NO",
            id_value="10-2024-1234567",
        )
    )
    db_session.add(
        MatterIdentifier(
            mid_id="id3",
            matter_id="m3",
            id_type="APP_NO",
            id_value="1020241234567",
        )
    )
    db_session.commit()

    out = _build_family_section(
        _make_ctx(
            mid="m1",
            matter=m1,
            overview=ov1,
            identifiers={"Priority": ["10-2024-1234567"]},
        )
    )

    mids = {r.get("matter_id") for r in (out.get("related_family_rows") or [])}
    assert mids == {"m2", "m3"}


def test_family_and_priority_link_match_with_normalized_app_no_alias(app, db_session):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0101")
    _m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="26PD0102")

    db_session.add(
        MatterIdentifier(
            mid_id="id1",
            matter_id="m1",
            id_type="Priority",
            id_value="10-2024-1234567",
        )
    )
    db_session.add(
        MatterIdentifier(
            mid_id="id2",
            matter_id="m2",
            id_type="APP_NO",
            id_value="1020241234567",
        )
    )
    db_session.commit()

    out = _build_family_section(
        _make_ctx(
            mid="m1",
            matter=m1,
            overview=ov1,
            identifiers={"Priority": ["10-2024-1234567"]},
        )
    )

    family_rows = out.get("related_family_rows") or []
    assert out.get("family_count") == 1
    assert len(family_rows) == 1
    assert family_rows[0]["matter_id"] == "m2"
    assert family_rows[0]["work_label"] == "US - Patent (Priority)"

    priority_rows = out.get("priority_rows") or []
    assert len(priority_rows) == 1
    assert priority_rows[0]["priority_no"] == "10-2024-1234567"
    assert priority_rows[0]["linked_case_id"] == "m2"


def test_priority_rows_hide_inaccessible_linked_case(app, db_session, monkeypatch):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0101")
    _m2, _ov2 = _add_matter(db_session, matter_id="m2", our_ref="26PD0102")

    db_session.add(
        MatterIdentifier(
            mid_id="id1",
            matter_id="m1",
            id_type="Priority",
            id_value="10-2024-1234567",
        )
    )
    db_session.add(
        MatterIdentifier(
            mid_id="id2",
            matter_id="m2",
            id_type="APP_NO",
            id_value="10-2024-1234567",
        )
    )
    db_session.commit()

    def _deny_related(_user, matter_id: str, action: str = "view") -> bool:
        return matter_id != "m2"

    monkeypatch.setattr("app.utils.permissions.can_access_matter", _deny_related)
    ctx = _make_ctx(
        mid="m1",
        matter=m1,
        overview=ov1,
        identifiers={"Priority": ["10-2024-1234567"]},
    )
    ctx["_current_user"] = object()

    out = _build_family_section(ctx)

    priority_rows = out.get("priority_rows") or []
    assert len(priority_rows) == 1
    assert priority_rows[0]["priority_no"] == "10-2024-1234567"
    assert priority_rows[0]["linked_case_id"] == ""
    assert priority_rows[0]["linked_our_ref"] == ""


def test_priority_rows_split_multi_value_identifier(app, db_session):
    m1, ov1 = _add_matter(db_session, matter_id="m1", our_ref="26PD0201")
    db_session.commit()

    out = _build_family_section(
        _make_ctx(
            mid="m1",
            matter=m1,
            overview=ov1,
            identifiers={"Priority": ["10-2024-1111111, 10-2024-2222222"]},
        )
    )

    priority_rows = out.get("priority_rows") or []
    assert [r.get("priority_no") for r in priority_rows] == [
        "10-2024-1111111",
        "10-2024-2222222",
    ]


def test_family_section_does_not_auto_link_cross_year_ref_series(app, db_session):
    m1, ov1 = _add_matter(
        db_session,
        matter_id="m1",
        our_ref="26PO0102US",
        right_group="OUT",
        matter_type="PATENT",
        right_name="Text-Text Text Text Text Text Text",
    )
    _m2, _ov2 = _add_matter(
        db_session,
        matter_id="m2",
        our_ref="25PO0102US",
        right_group="OUT",
        matter_type="PATENT",
        right_name="Text Text Text Text Text",
    )
    db_session.commit()

    out = _build_family_section(_make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={}))

    assert out.get("family_count") == 0
    assert (out.get("related_family_rows") or []) == []


def test_family_section_ref_series_does_not_link_with_different_titles(app, db_session):
    m1, ov1 = _add_matter(
        db_session,
        matter_id="m1",
        our_ref="24PD0104US",
        right_group="DOM",
        matter_type="PATENT",
        right_name="Text-Text Text Text Text Text Text",
    )
    _m2, _ov2 = _add_matter(
        db_session,
        matter_id="m2",
        our_ref="25PD0104US",
        right_group="DOM",
        matter_type="PATENT",
        right_name="Text Text Text Text Text Text Text",
    )
    db_session.commit()

    out = _build_family_section(_make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={}))

    assert out.get("family_count") == 0
    assert (out.get("related_family_rows") or []) == []


def test_family_section_excludes_auto_related_ids_via_custom_field(app, db_session):
    m1, ov1 = _add_matter(
        db_session,
        matter_id="m1",
        our_ref="26PO0102US",
        right_group="OUT",
        matter_type="PATENT",
        right_name="Text-Text Text Text Text Text Text",
    )
    _m2, _ov2 = _add_matter(
        db_session,
        matter_id="m2",
        our_ref="25PO0102US",
        right_group="OUT",
        matter_type="PATENT",
        right_name="Text Text Text Text Text",
    )
    db_session.add(
        MatterIdentifier(
            mid_id="id1",
            matter_id="m2",
            id_type="APP_NO",
            id_value="1020241234567",
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id="m1",
            namespace="family",
            data={"excluded_related_matter_ids": ["m2"]},
        )
    )
    db_session.commit()

    out = _build_family_section(
        _make_ctx(
            mid="m1",
            matter=m1,
            overview=ov1,
            identifiers={"Priority": ["10-2024-1234567"]},
        )
    )

    assert out.get("family_count") == 0
    assert (out.get("related_family_rows") or []) == []


def test_family_section_keeps_manual_family_even_when_auto_excluded(app, db_session):
    m1, ov1 = _add_matter(
        db_session,
        matter_id="m1",
        our_ref="26PO0102US",
        right_group="OUT",
        matter_type="PATENT",
        right_name="Text-Text Text Text Text Text Text",
    )
    _m2, _ov2 = _add_matter(
        db_session,
        matter_id="m2",
        our_ref="25PO0102US",
        right_group="OUT",
        matter_type="PATENT",
        right_name="Text Text Text Text Text",
    )
    db_session.add(Family(family_id="fam1", family_key="FAM-1"))
    db_session.add(MatterFamily(mf_id="mf1", matter_id="m1", family_id="fam1", link_role="manual"))
    db_session.add(MatterFamily(mf_id="mf2", matter_id="m2", family_id="fam1", link_role="manual"))
    db_session.add(
        MatterCustomField(
            matter_id="m1",
            namespace="family",
            data={"excluded_related_matter_ids": ["m2"]},
        )
    )
    db_session.commit()

    out = _build_family_section(_make_ctx(mid="m1", matter=m1, overview=ov1, identifiers={}))

    rows = out.get("related_family_rows") or []
    assert out.get("family_count") == 1
    assert len(rows) == 1
    assert rows[0]["matter_id"] == "m2"
    assert rows[0]["work_label"] == "Foreign - Patent (Family)"
