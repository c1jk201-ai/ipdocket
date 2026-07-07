from __future__ import annotations

import re
from typing import Callable

from app.extensions import db
from app.services.parameter_conflict.parameter_conflict_types import (
    ConflictItem,
    _normalize_identifier,
    _parse_date_str,
)
from app.utils.policy_sql import policy_text as text

_ALIAS_ID_TYPES = {
    "APP_NO": "Application No.",
    "REG_NO": "Registration No.",
    "PUB_NO": "Publication No.",
}

_BASIC_STAFF_KEYS = {"attorney", "manager", "handler"}
_IDENTIFIER_CUSTOM_FIELD_MAP = {
    "APP_NO": "application_no",
    "Application No.": "application_no",
    "application_no": "application_no",
    "app_no": "application_no",
    "REG_NO": "registration_no",
    "Registration No.": "registration_no",
    "registration_no": "registration_no",
    "reg_no": "registration_no",
    "PUB_NO": "publication_no",
    "Publication No.": "publication_no",
    "publication_no": "publication_no",
    "pub_no": "publication_no",
}
_EVENT_CUSTOM_FIELD_MAP = {
    "APP_DATE": "application_date",
    "Filing date": "application_date",
    "": "priority_date",
    "PRIORITY_DATE": "priority_date",
    "REG_DATE": "registration_date",
    "Registration date": "registration_date",
    "PUB_DATE": "publication_date",
    "Publication date": "publication_date",
}
_ROLE_CUSTOM_FIELD_MAP = {
    "inventor": "inventor_name",
}

# NOTE: matter   "" SQL   
#    .
_ALLOWED_MATTER_UPDATE_COLUMNS = {"right_name"}
_ALLOWED_TABLES = {
    "matter",
    "matter_custom_field",
    "matter_identifier",
    "matter_event",
    "matter_party_role",
    "matter_staff_assignment",
}
_FIELD_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")


def _is_date_event_key(event_key: str | None) -> bool:
    key = (event_key or "").strip()
    if not key:
        return False
    upper = key.upper()
    if upper.endswith("_DATE") or upper.endswith("_DEADLINE"):
        return True
    if key in _EVENT_CUSTOM_FIELD_MAP:
        return True
    if key == "":
        return True
    try:
        from app.services.matter.matter_auto_status import _RAW_EVENT_KEY_TO_STD_EVENT

        return key in _RAW_EVENT_KEY_TO_STD_EVENT
    except Exception:
        return False


def _upsert_custom_field(
    *,
    matter_id: str,
    namespace: str,
    field_key: str,
    value: str | None,
) -> None:
    if not (matter_id or "").strip():
        return
    if not (namespace or "").strip():
        return
    new_val = (value or "").strip()
    if not new_val:
        return

    import json

    existing = (
        db.session.execute(
            text(
                """
            SELECT id, data
            FROM matter_custom_field
            WHERE matter_id = :mid AND namespace = :ns
            """
            ),
            {"mid": matter_id, "ns": namespace},
        )
        .mappings()
        .first()
    )

    if existing:
        data_val = existing["data"] or "{}"
        if isinstance(data_val, str):
            try:
                new_data = json.loads(data_val)
            except Exception:
                new_data = {}
        else:
            new_data = data_val
        cur = (new_data.get(field_key) or "").strip() if isinstance(new_data, dict) else ""
        if cur == new_val:
            return
        if not isinstance(new_data, dict):
            new_data = {}
        new_data[field_key] = new_val
        db.session.execute(
            text("UPDATE matter_custom_field SET data = :data WHERE id = :id"),
            {"data": json.dumps(new_data), "id": existing["id"]},
        )
        return

    db.session.execute(
        text(
            """
            INSERT INTO matter_custom_field (matter_id, namespace, data)
            VALUES (:mid, :ns, :data)
            """
        ),
        {"mid": matter_id, "ns": namespace, "data": json.dumps({field_key: new_val})},
    )


def _derive_exam_requested(raw_text: str | None) -> str | None:
    text_val = (raw_text or "").strip()
    if not text_val:
        return None
    upper_val = text_val.upper()
    if upper_val in {"Y", "YES", "TRUE", "1"}:
        return "Y"
    if upper_val in {"N", "NO", "FALSE", "0"}:
        return "N"
    if "Billing" in text_val:
        return "N"
    if "Billing" in text_val:
        return "Y"
    return None


def _sync_party_role_custom_field(
    *,
    matter_id: str,
    namespace: str,
    role_code: str,
    field_key: str,
) -> None:
    rows = (
        db.session.execute(
            text(
                """
            SELECT COALESCE(p.name_display, r.raw_text, '')
            FROM matter_party_role r
            LEFT JOIN party p ON p.party_id = r.party_id
            WHERE r.matter_id = :mid
              AND lower(r.role_code) = :code
            ORDER BY COALESCE(r.seq, 0) ASC, r.mpr_id ASC
            """
            ),
            {"mid": matter_id, "code": role_code.lower()},
        )
        .scalars()
        .all()
    )

    names = []
    seen = set()
    for name in rows or []:
        clean = (name or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        names.append(clean)

    if not names:
        return

    _upsert_custom_field(
        matter_id=matter_id,
        namespace=namespace,
        field_key=field_key,
        value="; ".join(names),
    )


def _sync_basic_staff_field(*, matter_id: str, role_code: str, value: str) -> None:
    if not (matter_id or "").strip():
        return
    if role_code not in _BASIC_STAFF_KEYS:
        return
    name = (value or "").strip()
    if not name:
        return

    import json

    existing = (
        db.session.execute(
            text(
                """
            SELECT id, data
            FROM matter_custom_field
            WHERE matter_id = :mid AND namespace = 'basic'
            """
            ),
            {"mid": matter_id},
        )
        .mappings()
        .first()
    )

    if existing:
        data_val = existing["data"] or "{}"
        if isinstance(data_val, str):
            new_data = json.loads(data_val)
        else:
            new_data = data_val
        if (new_data.get(role_code) or "").strip():
            return
        new_data[role_code] = name
        db.session.execute(
            text("UPDATE matter_custom_field SET data = :data WHERE id = :id"),
            {"data": json.dumps(new_data), "id": existing["id"]},
        )
    else:
        new_data = {role_code: name}
        db.session.execute(
            text(
                """
                INSERT INTO matter_custom_field (matter_id, namespace, data)
                VALUES (:mid, 'basic', :data)
                """
            ),
            {"mid": matter_id, "data": json.dumps(new_data)},
        )


def apply_field(
    *,
    matter_id: str,
    item: ConflictItem,
    get_custom_field_namespace: Callable[[], str],
) -> None:
    import json
    import uuid

    table = (item.table_name or "").strip()
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"invalid_table_name:{table}")
    key = (item.field_key or "").strip()
    if not key:
        raise ValueError("empty_field_key")
    if table != "matter" and not _FIELD_KEY_RE.match(key):
        raise ValueError(f"invalid_field_key:{key}")

    if item.table_name == "matter":
        # SECURITY/P0: item.field_key External Input()from     Required
        if key not in _ALLOWED_MATTER_UPDATE_COLUMNS:
            raise ValueError(f"invalid_matter_field_key:{key}")
        # Keep ORM identity map in sync (tests and UI may read the object again in the same session).
        from app.models.ip_records import Matter

        Matter.query.filter_by(matter_id=matter_id).update(
            {"right_name": item.new_value},
            synchronize_session="fetch",
        )
        return

    if item.table_name == "matter_custom_field":
        namespace = get_custom_field_namespace()

        existing = (
            db.session.execute(
                text(
                    """
                    SELECT id, data FROM matter_custom_field
                    WHERE matter_id = :mid AND namespace = :ns
                """
                ),
                {"mid": matter_id, "ns": namespace},
            )
            .mappings()
            .first()
        )

        if existing:
            data_val = existing["data"] or "{}"
            if isinstance(data_val, str):
                new_data = json.loads(data_val)
            else:
                new_data = data_val

            new_data[item.field_key] = item.new_value
            db.session.execute(
                text(
                    """
                        UPDATE matter_custom_field SET data = :data
                        WHERE id = :id
                    """
                ),
                {"data": json.dumps(new_data), "id": existing["id"]},
            )
        else:
            new_data = {item.field_key: item.new_value}
            db.session.execute(
                text(
                    """
                        INSERT INTO matter_custom_field (matter_id, namespace, data)
                        VALUES (:mid, :ns, :data)
                    """
                ),
                {"mid": matter_id, "ns": namespace, "data": json.dumps(new_data)},
            )
        return

    if item.table_name == "matter_identifier":
        multi_id_types = {"Priority", "Parent application No."}
        is_multi = item.field_key in multi_id_types
        norm_new = _normalize_identifier(item.new_value)

        if is_multi:
            existing = (
                db.session.execute(
                    text(
                        """
                        SELECT id_value
                        FROM matter_identifier
                        WHERE matter_id = :mid AND id_type = :id_type
                    """
                    ),
                    {"mid": matter_id, "id_type": item.field_key},
                )
                .scalars()
                .all()
            )

            for ex_val in existing or []:
                if _normalize_identifier(ex_val) == norm_new:
                    return

            db.session.execute(
                text(
                    """
                        INSERT INTO matter_identifier (mid_id, matter_id, id_type, id_value)
                        VALUES (:id, :mid, :id_type, :val)
                    """
                ),
                {
                    "id": uuid.uuid4().hex,
                    "mid": matter_id,
                    "id_type": item.field_key,
                    "val": item.new_value,
                },
            )
            return

        alias = _ALIAS_ID_TYPES.get(item.field_key)
        if alias:
            existing_row = (
                db.session.execute(
                    text(
                        """
                        SELECT mid_id, id_type
                        FROM matter_identifier
                        WHERE matter_id = :mid AND id_type IN (:id_type, :alias)
                    """
                    ),
                    {"mid": matter_id, "id_type": item.field_key, "alias": alias},
                )
                .mappings()
                .first()
            )
        else:
            existing_row = (
                db.session.execute(
                    text(
                        """
                        SELECT mid_id, id_type
                        FROM matter_identifier
                        WHERE matter_id = :mid AND id_type = :id_type
                    """
                    ),
                    {"mid": matter_id, "id_type": item.field_key},
                )
                .mappings()
                .first()
            )

        if existing_row:
            db.session.execute(
                text(
                    """
                        UPDATE matter_identifier
                        SET id_type = :id_type, id_value = :val
                        WHERE mid_id = :id
                    """
                ),
                {
                    "id_type": item.field_key,
                    "val": item.new_value,
                    "id": existing_row["mid_id"],
                },
            )
        else:
            db.session.execute(
                text(
                    """
                        INSERT INTO matter_identifier (mid_id, matter_id, id_type, id_value)
                        VALUES (:id, :mid, :id_type, :val)
                    """
                ),
                {
                    "id": uuid.uuid4().hex,
                    "mid": matter_id,
                    "id_type": item.field_key,
                    "val": item.new_value,
                },
            )
        custom_key = _IDENTIFIER_CUSTOM_FIELD_MAP.get(item.field_key)
        if custom_key:
            ns = get_custom_field_namespace()
            _upsert_custom_field(
                matter_id=matter_id,
                namespace=ns,
                field_key=custom_key,
                value=item.new_value,
            )
        return

    if item.table_name == "matter_event":
        existing = db.session.execute(
            text(
                """
                    SELECT mevent_id FROM matter_event
                    WHERE matter_id = :mid AND event_key = :key
                """
            ),
            {"mid": matter_id, "key": item.field_key},
        ).scalar()

        is_date = _is_date_event_key(item.field_key)
        parsed_event_date = _parse_date_str(item.new_value) if is_date else None

        if existing:
            if is_date:
                db.session.execute(
                    text(
                        """
                            UPDATE matter_event
                            SET event_at = :val,
                                event_date = :event_date
                            WHERE mevent_id = :id
                        """
                    ),
                    {
                        "val": item.new_value,
                        "event_date": parsed_event_date,
                        "id": existing,
                    },
                )
            else:
                try:
                    db.session.execute(
                        text(
                            """
                                UPDATE matter_event SET raw_text = :val
                                WHERE mevent_id = :id
                            """
                        ),
                        {"val": item.new_value, "id": existing},
                    )
                except Exception:
                    # Backward-compat: store "raw_text" events in event_at on older schemas.
                    db.session.execute(
                        text(
                            """
                                UPDATE matter_event SET event_at = :val
                                WHERE mevent_id = :id
                            """
                        ),
                        {"val": item.new_value, "id": existing},
                    )
        else:
            if is_date:
                db.session.execute(
                    text(
                        """
                            INSERT INTO matter_event (
                                mevent_id, matter_id, event_key, event_at, event_date
                            )
                            VALUES (:id, :mid, :key, :val, :event_date)
                        """
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "mid": matter_id,
                        "key": item.field_key,
                        "val": item.new_value,
                        "event_date": parsed_event_date,
                    },
                )
            else:
                try:
                    db.session.execute(
                        text(
                            """
                                INSERT INTO matter_event (mevent_id, matter_id, event_key, raw_text)
                                VALUES (:id, :mid, :key, :val)
                            """
                        ),
                        {
                            "id": uuid.uuid4().hex,
                            "mid": matter_id,
                            "key": item.field_key,
                            "val": item.new_value,
                        },
                    )
                except Exception:
                    # Backward-compat: store "raw_text" events in event_at on older schemas.
                    db.session.execute(
                        text(
                            """
                                INSERT INTO matter_event (mevent_id, matter_id, event_key, event_at)
                                VALUES (:id, :mid, :key, :val)
                            """
                        ),
                        {
                            "id": uuid.uuid4().hex,
                            "mid": matter_id,
                            "key": item.field_key,
                            "val": item.new_value,
                        },
                    )
        ns = get_custom_field_namespace()
        custom_key = _EVENT_CUSTOM_FIELD_MAP.get(item.field_key)
        if custom_key:
            _upsert_custom_field(
                matter_id=matter_id,
                namespace=ns,
                field_key=custom_key,
                value=item.new_value,
            )
        if item.field_key == "EXAM_REQ":
            exam_requested = _derive_exam_requested(item.new_value)
            if exam_requested:
                _upsert_custom_field(
                    matter_id=matter_id,
                    namespace=ns,
                    field_key="exam_requested",
                    value=exam_requested,
                )
        return

    if item.table_name == "matter_party_role":
        role_code = (item.field_key or "").strip().lower()
        if not role_code:
            return
        max_seq = (
            db.session.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) FROM matter_party_role WHERE matter_id = :mid AND lower(role_code) = :code"
                ),
                {"mid": matter_id, "code": role_code},
            ).scalar()
            or 0
        )

        db.session.execute(
            text(
                """
                    INSERT INTO matter_party_role (mpr_id, matter_id, role_code, raw_text, seq)
                    VALUES (:id, :mid, :code, :val, :seq)
                """
            ),
            {
                "id": uuid.uuid4().hex,
                "mid": matter_id,
                "code": role_code,
                "val": item.new_value,
                "seq": max_seq + 1,
            },
        )
        custom_key = _ROLE_CUSTOM_FIELD_MAP.get(role_code)
        if custom_key:
            ns = get_custom_field_namespace()
            _sync_party_role_custom_field(
                matter_id=matter_id,
                namespace=ns,
                role_code=role_code,
                field_key=custom_key,
            )
        return

    if item.table_name == "matter_staff_assignment":
        role_code = (item.field_key or "").strip().lower()
        if not role_code:
            return
        existing = db.session.execute(
            text(
                """
                    SELECT msa_id FROM matter_staff_assignment
                    WHERE matter_id = :mid AND lower(staff_role_code) = :code
                """
            ),
            {"mid": matter_id, "code": role_code},
        ).scalar()

        staff_party_id = db.session.execute(
            text(
                """
                SELECT staff_party_id
                  FROM users
                 WHERE staff_party_id IS NOT NULL
                   AND TRIM(staff_party_id) <> ''
                   AND LOWER(TRIM(display_name)) = LOWER(TRIM(:name))
                 ORDER BY id DESC
                 LIMIT 1
                """
            ),
            {"name": (item.new_value or "").strip()},
        ).scalar()

        if existing:
            db.session.execute(
                text(
                    """
                    UPDATE matter_staff_assignment
                    SET staff_role_code = :code,
                        raw_text = :val,
                        staff_party_id = COALESCE(:sid, staff_party_id)
                    WHERE msa_id = :id
                    """
                ),
                {"val": item.new_value, "id": existing, "code": role_code, "sid": staff_party_id},
            )
            _sync_basic_staff_field(matter_id=matter_id, role_code=role_code, value=item.new_value)
            return

        if not staff_party_id:
            _sync_basic_staff_field(matter_id=matter_id, role_code=role_code, value=item.new_value)
            return

        db.session.execute(
            text(
                """
                    INSERT INTO matter_staff_assignment (msa_id, matter_id, staff_role_code, staff_party_id, raw_text)
                    VALUES (:id, :mid, :code, :sid, :val)
                """
            ),
            {
                "id": uuid.uuid4().hex,
                "mid": matter_id,
                "code": role_code,
                "sid": staff_party_id,
                "val": item.new_value,
            },
        )
        _sync_basic_staff_field(matter_id=matter_id, role_code=role_code, value=item.new_value)
        return


def apply_parameters(
    *,
    matter_id: str,
    auto_apply: list[ConflictItem],
    user_selections: dict[str, str],
    conflicts: list[ConflictItem] | None,
    get_custom_field_namespace: Callable[[], str],
) -> dict:
    applied = []
    skipped = []

    conflict_map = {item.field_name: item for item in (conflicts or [])}

    for item in auto_apply:
        if item.table_name == "matter_staff_assignment":
            skipped.append(item.field_name)
            continue
        if item.new_value:
            apply_field(
                matter_id=matter_id,
                item=item,
                get_custom_field_namespace=get_custom_field_namespace,
            )
            applied.append(item.field_label)

    for field_name, choice in user_selections.items():
        if choice == "new":
            item = conflict_map.get(field_name)
            if item and item.new_value:
                if item.table_name == "matter_staff_assignment":
                    skipped.append(field_name)
                    continue
                apply_field(
                    matter_id=matter_id,
                    item=item,
                    get_custom_field_namespace=get_custom_field_namespace,
                )
                applied.append(item.field_label)
        else:
            skipped.append(field_name)

    return {"applied": applied, "skipped": skipped}
