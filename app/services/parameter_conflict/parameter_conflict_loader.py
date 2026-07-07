from sqlalchemy import inspect

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


def load_matter_data(*, matter_id: str) -> dict:
    matter = (
        db.session.execute(
            text("SELECT * FROM matter WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        .mappings()
        .first()
    )

    if not matter:
        return {}

    data = dict(matter)

    identifiers = (
        db.session.execute(
            text(
                """
                SELECT id_type, id_value
                FROM matter_identifier
                WHERE matter_id = :mid
            """
            ),
            {"mid": matter_id},
        )
        .mappings()
        .all()
    )

    data["identifiers"] = {}
    for r in identifiers:
        k = r["id_type"]
        v = r["id_value"]
        if k not in data["identifiers"]:
            data["identifiers"][k] = []
        data["identifiers"][k].append(v)

    has_event_raw_text = True
    try:
        insp = inspect(db.engine)
        if insp.has_table("matter_event"):
            cols = {c["name"] for c in insp.get_columns("matter_event")}
            has_event_raw_text = "raw_text" in cols
    except Exception:
        has_event_raw_text = True

    if has_event_raw_text:
        events = (
            db.session.execute(
                text(
                    """
                    SELECT event_key, event_at, raw_text
                    FROM matter_event
                    WHERE matter_id = :mid
                """
                ),
                {"mid": matter_id},
            )
            .mappings()
            .all()
        )
    else:
        # Backward-compat: older DBs don't have matter_event.raw_text.
        events = (
            db.session.execute(
                text(
                    """
                    SELECT event_key, event_at, NULL AS raw_text
                    FROM matter_event
                    WHERE matter_id = :mid
                """
                ),
                {"mid": matter_id},
            )
            .mappings()
            .all()
        )

    data["events"] = {r["event_key"]: r["event_at"] or r["raw_text"] for r in events}

    party_roles = (
        db.session.execute(
            text(
                """
                SELECT role_code, raw_text, seq
                FROM matter_party_role
                WHERE matter_id = :mid
                ORDER BY role_code, seq
            """
            ),
            {"mid": matter_id},
        )
        .mappings()
        .all()
    )

    roles_by_code: dict[str, list] = {}
    for r in party_roles:
        code = r["role_code"]
        if code not in roles_by_code:
            roles_by_code[code] = []
        roles_by_code[code].append(r["raw_text"])
    data["party_roles"] = roles_by_code

    custom_fields = (
        db.session.execute(
            text(
                """
                SELECT namespace, data
                FROM matter_custom_field
                WHERE matter_id = :mid
            """
            ),
            {"mid": matter_id},
        )
        .mappings()
        .all()
    )

    merged_data: dict = {}
    for row in custom_fields:
        if row["data"]:
            try:
                import json

                d = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                if isinstance(d, dict):
                    merged_data.update(d)
            except Exception as exc:
                # Best-effort: ignore malformed custom_field blobs.
                report_swallowed_exception(
                    exc,
                    context="parameter_conflict_loader.load_matter_data.custom_fields_json",
                    log_key="parameter_conflict_loader.load_matter_data.custom_fields_json",
                    log_window_seconds=300,
                )
    data["custom_fields"] = merged_data

    staff = (
        db.session.execute(
            text(
                """
                SELECT msa.staff_role_code, msa.raw_text, p.name_display
                FROM matter_staff_assignment msa
                LEFT JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                LEFT JOIN party p ON p.party_id = ps.party_id
                WHERE msa.matter_id = :mid
            """
            ),
            {"mid": matter_id},
        )
        .mappings()
        .all()
    )

    data["staff"] = {r["staff_role_code"]: r["name_display"] or r["raw_text"] for r in staff}

    return data
