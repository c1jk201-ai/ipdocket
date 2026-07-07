import uuid


def _insert_status_display_case(db_session, *, label: str, done_date: str | None = None):
    from app.models.ip_records import DocketItem, Matter, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=f"TEST-STATUS-SEARCH-{matter_id[:8]}",
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
            inhouse_status="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=f"TEST-STATUS-SEARCH-{matter_id[:8]}",
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
            inhouse_status="",
            clients="",
            applicants="",
            attorneys="",
            entered_at="2026-06-27",
        )
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=matter_id,
            category="MGMT",
            name_ref=f"MGMT:STATUS_RED:{label}",
            name_free=label,
            due_date="2026-07-31",
            done_date=done_date,
            is_deleted=False,
        )
    )
    db_session.commit()
    return matter_id


def test_case_list_status_search_matches_open_display_status_red_alias(db_session):
    from app.blueprints.case.routes.list import _status_display_match_exists
    from app.models.ip_records import VMatterOverview

    matter_id = _insert_status_display_case(db_session, label="Examination requestDeadline")

    rows = (
        VMatterOverview.query.with_entities(VMatterOverview.matter_id)
        .filter(
            _status_display_match_exists(
                VMatterOverview.matter_id,
                "Examination Billing In Progress",
            )
        )
        .all()
    )

    assert matter_id in {row[0] for row in rows}


def test_case_list_status_search_matches_matter_query_display_status_red_alias(db_session):
    from app.blueprints.case.routes.list import _status_display_match_exists
    from app.models.ip_records import Matter

    matter_id = _insert_status_display_case(db_session, label="Examination requestDeadline")

    rows = (
        Matter.query.with_entities(Matter.matter_id)
        .filter(_status_display_match_exists(Matter.matter_id, "ExaminationBilling"))
        .all()
    )

    assert matter_id in {row[0] for row in rows}


def test_case_list_status_search_ignores_closed_display_status_red(db_session):
    from app.blueprints.case.routes.list import _status_display_match_exists
    from app.models.ip_records import VMatterOverview

    matter_id = _insert_status_display_case(
        db_session,
        label="Examination requestDeadline",
        done_date="2026-06-27",
    )

    rows = (
        VMatterOverview.query.with_entities(VMatterOverview.matter_id)
        .filter(_status_display_match_exists(VMatterOverview.matter_id, "ExaminationBilling"))
        .all()
    )

    assert matter_id not in {row[0] for row in rows}
