from app.services.ops.data_quality import get_dummy_candidates


def _ids(rows):
    return {r.get("matter_id") for r in (rows or [])}


def test_dummy_candidates_excludes_closed_status_red(db_session):
    from app.models.matter import Matter

    m = Matter(
        matter_id="m_closed_1",
        our_ref="T0001US",
        right_name="Not a dummy",
        right_group="DOM",
        matter_type="PATENT",
        retained_at="2020-01-01",
        status_red="Text",
        is_deleted=False,
    )
    db_session.add(m)
    db_session.commit()

    res = get_dummy_candidates()
    assert "m_closed_1" not in _ids(res)


def test_dummy_candidates_skips_zombie_for_litigation(db_session):
    from app.models.matter import Matter

    m = Matter(
        matter_id="m_lit_1",
        our_ref="C240001US",
        right_name="Text Text",
        matter_type="LITIGATION",
        retained_at="2020-01-01",
        inhouse_status="Text",
        is_deleted=False,
    )
    db_session.add(m)
    db_session.commit()

    res = get_dummy_candidates()
    assert "m_lit_1" not in _ids(res)


def test_dummy_candidates_still_flags_litigation_with_test_keyword(db_session):
    from app.models.matter import Matter

    m = Matter(
        matter_id="m_lit_2",
        our_ref="C240002US",
        right_name="test warning letter",
        matter_type="LITIGATION",
        retained_at="2020-01-01",
        is_deleted=False,
    )
    db_session.add(m)
    db_session.commit()

    res = get_dummy_candidates()
    assert "m_lit_2" in _ids(res)


def test_dummy_candidates_skips_zombie_for_etc_copyright(db_session):
    from app.models.matter import Matter

    m = Matter(
        matter_id="m_copyright_1",
        our_ref="26ET0001ETC",
        right_name="Text Text Text",
        right_group="ETC",
        matter_type="COPYRIGHT",
        retained_at="2020-01-01",
        is_deleted=False,
    )
    db_session.add(m)
    db_session.commit()

    res = get_dummy_candidates()
    assert "m_copyright_1" not in _ids(res)


def test_dummy_candidates_batches_identifier_and_communication_queries(db_session):
    from sqlalchemy import event

    from app.extensions import db
    from app.models.communication import Communication
    from app.models.matter import Matter, MatterIdentifier

    db_session.add_all(
        [
            Matter(
                matter_id="m_batch_real_id",
                our_ref="BATCH001",
                right_name="Text Text",
                matter_type="PATENT",
                retained_at="2020-01-01",
                is_deleted=False,
            ),
            Matter(
                matter_id="m_batch_with_comm",
                our_ref="BATCH002",
                right_name="Text Text",
                matter_type="PATENT",
                retained_at="2020-01-01",
                is_deleted=False,
            ),
            Matter(
                matter_id="m_batch_zombie",
                our_ref="BATCH003",
                right_name="Text Text",
                matter_type="PATENT",
                retained_at="2020-01-01",
                is_deleted=False,
            ),
        ]
    )
    db_session.add(
        MatterIdentifier(
            matter_id="m_batch_real_id",
            id_type="APP_NO",
            id_value="10-2020-1234567",
        )
    )
    db_session.add(
        Communication(
            comm_id="comm_batch_1",
            matter_id="m_batch_with_comm",
            comm_type="R",
            received_date="2020-02-01",
        )
    )
    db_session.commit()

    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    event.listen(db.engine, "before_cursor_execute", record_sql)
    try:
        res = get_dummy_candidates()
    finally:
        event.remove(db.engine, "before_cursor_execute", record_sql)

    ids = _ids(res)
    assert "m_batch_real_id" not in ids
    assert "m_batch_with_comm" not in ids
    assert "m_batch_zombie" in ids

    identifier_selects = [s for s in statements if "from matter_identifier" in s]
    communication_selects = [s for s in statements if "from communication" in s]
    assert len(identifier_selects) == 1
    assert len(communication_selects) == 1
