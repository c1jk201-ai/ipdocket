from __future__ import annotations

import uuid

from app.blueprints.case.routes.list import _case_kind_filter_expr
from app.models.ip_records import Matter


def _make_matter(
    *, our_ref: str, right_group: str | None, matter_type: str, right_name: str
) -> Matter:
    return Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=our_ref,
        right_group=right_group,
        matter_type=matter_type,
        right_name=right_name,
        retained_at="2026-03-18",
        entered_at="2026-03-18",
    )


def test_special_case_lists_follow_explicit_etc_storage(db_session) -> None:
    assert (
        _case_kind_filter_expr(
            Matter.right_group,
            Matter.matter_type,
            division_code=None,
            type_code=None,
        )
        is None
    )
    assert (
        _case_kind_filter_expr(
            Matter.right_group,
            Matter.matter_type,
            division_code="",
            type_code="",
        )
        is None
    )

    madrid = _make_matter(
        our_ref="26TO0101US",
        right_group="ETC",
        matter_type="MADRID",
        right_name="Madrid Matter",
    )
    hague = _make_matter(
        our_ref="26DO0101US",
        right_group="ETC",
        matter_type="HAGUE",
        right_name="Hague Matter",
    )
    pct = _make_matter(
        our_ref="26PD0101PCT",
        right_group="ETC",
        matter_type="PCT",
        right_name="PCT Matter",
    )
    copyright = _make_matter(
        our_ref="26ET0101US",
        right_group="ETC",
        matter_type="COPYRIGHT",
        right_name="Copyright Matter",
    )
    misc = _make_matter(
        our_ref="26ET0102US",
        right_group="ETC",
        matter_type="MISC",
        right_name="Misc Matter",
    )
    madrid_ref = madrid.our_ref
    hague_ref = hague.our_ref
    pct_ref = pct.our_ref
    copyright_ref = copyright.our_ref
    misc_ref = misc.our_ref
    db_session.add_all([madrid, hague, pct, copyright, misc])
    db_session.commit()

    madrid_refs = {
        row.our_ref
        for row in Matter.query.filter(
            _case_kind_filter_expr(
                Matter.right_group,
                Matter.matter_type,
                division_code="ETC",
                type_code="MADRID",
            )
        ).all()
    }
    assert madrid_ref in madrid_refs

    hague_refs = {
        row.our_ref
        for row in Matter.query.filter(
            _case_kind_filter_expr(
                Matter.right_group,
                Matter.matter_type,
                division_code="ETC",
                type_code="HAGUE",
            )
        ).all()
    }
    assert hague_ref in hague_refs

    pct_refs = {
        row.our_ref
        for row in Matter.query.filter(
            _case_kind_filter_expr(
                Matter.right_group,
                Matter.matter_type,
                division_code="ETC",
                type_code="PCT",
            )
        ).all()
    }
    assert pct_ref in pct_refs

    copyright_refs = {
        row.our_ref
        for row in Matter.query.filter(
            _case_kind_filter_expr(
                Matter.right_group,
                Matter.matter_type,
                division_code="ETC",
                type_code="COPYRIGHT",
            )
        ).all()
    }
    assert copyright_ref in copyright_refs

    misc_refs = {
        row.our_ref
        for row in Matter.query.filter(
            _case_kind_filter_expr(
                Matter.right_group,
                Matter.matter_type,
                division_code="ETC",
                type_code="MISC",
            )
        ).all()
    }
    assert misc_ref in misc_refs
    assert copyright_ref not in misc_refs
