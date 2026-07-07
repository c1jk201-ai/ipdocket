from app.blueprints.case.helpers import (
    _clear_duplicate_appeal_no,
    _sync_matter_identifiers_from_inc_patent,
)
from app.models.ip_records import MatterIdentifier
from app.utils.policy_sql import policy_text as text


def test_inc_patent_sync_drops_appeal_no_when_it_equals_application_no(
    db_session,
    sample_matter,
):
    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    db_session.add(
        MatterIdentifier(
            matter_id=mid,
            id_type="Appeal No.",
            id_value="10-2026-7012086",
            source_column="incoming_patent",
        )
    )
    db_session.commit()

    _sync_matter_identifiers_from_inc_patent(
        matter_id=mid,
        inc_patent={
            "application_no": "10-2026-7012086",
            "appeal_no": "10-2026-7012086",
            "pct_application_no": "PCT/US2024/054280",
        },
    )
    db_session.commit()

    rows = db_session.execute(
        text(
            """
            SELECT id_type, id_value
            FROM matter_identifier
            WHERE matter_id = :mid
            ORDER BY id_type
            """
        ),
        {"mid": mid},
    ).fetchall()
    id_map = {id_type: id_value for id_type, id_value in rows}

    assert id_map.get("Application No.") == "10-2026-7012086"
    assert id_map.get("PCT Application No.") == "PCT/US2024/054280"
    assert "Appeal No." not in id_map


def test_inc_patent_sync_keeps_distinct_appeal_no(db_session, sample_matter):
    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    _sync_matter_identifiers_from_inc_patent(
        matter_id=mid,
        inc_patent={
            "application_no": "10-2026-7012086",
            "appeal_no": "20-2610-0000567",
        },
    )
    db_session.commit()

    rows = db_session.execute(
        text(
            """
            SELECT id_type, id_value
            FROM matter_identifier
            WHERE matter_id = :mid
              AND id_type IN ('Application No.', 'Appeal No.')
            """
        ),
        {"mid": mid},
    ).fetchall()
    id_map = {id_type: id_value for id_type, id_value in rows}

    assert id_map.get("Application No.") == "10-2026-7012086"
    assert id_map.get("Appeal No.") == "20-2610-0000567"


def test_clear_duplicate_appeal_no_blanks_custom_field_value():
    data = {
        "application_no": "10-2026-7012086",
        "appeal_no": "10-2026-7012086",
    }

    _clear_duplicate_appeal_no(data)

    assert data["appeal_no"] == ""


def test_clear_duplicate_appeal_no_keeps_distinct_trial_number():
    data = {
        "application_no": "10-2026-7012086",
        "appeal_no": "20-2610-0000567",
    }

    _clear_duplicate_appeal_no(data)

    assert data["appeal_no"] == "20-2610-0000567"
