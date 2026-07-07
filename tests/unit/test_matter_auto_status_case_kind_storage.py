from __future__ import annotations

from app.models.ip_records import Matter
from app.services.matter.matter_auto_status import _default_blue_for_matter, _get_matter_context


def test_matter_auto_status_maps_etc_madrid_to_outgoing_trademark(db_session):
    matter = Matter(
        matter_id="mid_status_etc_madrid",
        our_ref="26TO0001US",
        right_group="ETC",
        matter_type="MADRID",
        is_deleted=False,
    )
    db_session.add(matter)
    db_session.commit()

    ctx = _get_matter_context(matter.matter_id)

    assert ctx.division == "OUT"
    assert ctx.matter_type == "TRADEMARK"
    assert ctx.is_uspto is False


def test_default_blue_for_matter_uses_profile_type_for_etc_hague(db_session):
    matter = Matter(
        matter_id="mid_status_etc_hague",
        our_ref="26DO0001US",
        right_group="ETC",
        matter_type="HAGUE",
        is_deleted=False,
    )
    db_session.add(matter)
    db_session.commit()

    assert _default_blue_for_matter(matter.matter_id) == "Filing  In Progress"
