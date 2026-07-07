"""
Canonical Field Service

Provides canonical field resolution and case_flat_index management.
Canonical fields (attorney, manager, handler, etc.) are stored in different
namespaces per case type. This service:
1. Resolves canonical_key to actual storage location per case type
2. Computes flat index values for a case
3. Upserts case_flat_index on case save/update
"""

import csv
import json
import os
import re
from datetime import datetime
from functools import lru_cache
from typing import Any, Optional

from flask import current_app, has_app_context

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.ip_records import Matter, MatterCustomField, MatterIdentifier, VMatterOverview
from app.models.user import User
from app.services.case.case_kind import PATENT_LIKE_TYPES, _infer_case_kind
from app.services.case.case_parameter_service import CaseParameterService
from app.utils.search import compact_search_text as to_compact_compact
from app.utils.policy_sql import policy_text as text

# Key canonical fields to index
INDEXED_CANONICAL_FIELDS = [
    "attorney",
    "manager",
    "handler",
    "drawing_contact",
    "application_no",
    "registration_no",
    "publication_no",
    "applicant",
    "client_name",
    "application_date",
    "registration_date",
    "priority_date",
    "department",
    "status_internal",
    "inventor",
]


def _data_file_candidates(filename: str) -> list[str]:
    if has_app_context():
        candidates = [os.path.join(current_app.root_path, "data", filename)]
        base_dir = current_app.config.get("BASE_DIR") or ""
        if base_dir:
            candidates.append(os.path.join(base_dir, "app", "data", filename))
        return candidates

    app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return [os.path.join(app_root, "data", filename)]


@lru_cache(maxsize=1)
def load_canonical_config() -> dict:
    """
    Load and parse canonical_fields_extended.csv.
    Returns dict: {canonical_key: [target_dicts]}
    Each target_dict contains: business_area, division_code, case_type_code,
                               namespace, param_key, storage
    """
    csv_path = next(
        (
            path
            for path in _data_file_candidates("canonical_fields_extended.csv")
            if os.path.exists(path)
        ),
        "",
    )

    config = {}
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = row.get("canonical_key", "").strip()
                if not key:
                    continue

                targets_json = row.get("targets_json", "")
                try:
                    targets = json.loads(targets_json) if targets_json else []
                except json.JSONDecodeError:
                    targets = []

                config[key] = targets
    except FileNotFoundError:
        if has_app_context():
            current_app.logger.warning("canonical_fields_extended.csv not found")
        config = _load_canonical_config_from_parameter_mapping()

    return config


def _load_canonical_config_from_parameter_mapping() -> dict:
    """Build the flat-index mapping from the mounted parameter mapping CSV.

    The parameter mapping CSV carries the same namespace/param metadata and is
    mounted under ``app/data``.
    """
    candidates = _data_file_candidates("case_parameter_mapping.csv")
    csv_path = next((path for path in candidates if path and os.path.exists(path)), "")
    if not csv_path:
        if has_app_context():
            current_app.logger.warning("case_parameter_mapping.csv fallback not found")
        return {}

    canonical_param_aliases: dict[str, set[str]] = {
        "attorney": {"attorney"},
        "manager": {"manager"},
        "handler": {"handler"},
        "drawing_contact": {"drawing_contact", "drawing_handler"},
        "application_no": {
            "application_no",
            "basic_application_no",
            "parent_application_no",
            "pct_application_no",
            "ep_application_no",
            "ctm_application_no",
            "madrid_application_no",
        },
        "registration_no": {"registration_no", "basic_registration_no"},
        "publication_no": {"publication_no", "gazette_no"},
        "applicant": {"applicant_name", "application_applicant_name", "applicant_registrant"},
        "client_name": {"client_name"},
        "application_date": {
            "application_date",
            "basic_application_date",
            "foreign_filing_date",
            "parent_application_date",
            "pct_application_date",
            "ep_application_date",
            "ctm_application_date",
            "madrid_application_date",
        },
        "registration_date": {"registration_date"},
        "priority_date": {"priority_date"},
        "department": {"department"},
        "status_internal": {"inhouse_status", "status_internal"},
        "inventor": {"inventor_name", "inventor"},
    }
    alias_to_canonical = {
        alias: canonical_key
        for canonical_key, aliases in canonical_param_aliases.items()
        for alias in aliases
    }

    fallback: dict[str, list[dict[str, Any]]] = {key: [] for key in INDEXED_CANONICAL_FIELDS}
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_key = (row.get("param_key") or "").strip()
                namespace = (row.get("namespace") or "").strip()
                if not param_key or not namespace or namespace == "form":
                    continue
                canonical_key = alias_to_canonical.get(param_key)
                if not canonical_key:
                    continue
                fallback.setdefault(canonical_key, []).append(
                    {
                        "business_area": (row.get("business_area") or "").strip(),
                        "division_code": (row.get("division_code") or "").strip(),
                        "case_type_code": (row.get("case_type_code") or "").strip(),
                        "namespace": namespace,
                        "param_key": param_key,
                        "storage": (row.get("storage") or "").strip(),
                        "required": (row.get("required") or "").strip(),
                    }
                )
    except Exception as exc:
        if has_app_context():
            current_app.logger.warning(
                "Failed to load canonical fallback from %s: %s", csv_path, exc
            )
        return {}

    return {key: targets for key, targets in fallback.items() if targets}


def get_namespace_for_matter(matter: Matter) -> Optional[str]:
    """
    Determine the primary namespace for a matter based on its type.
    Returns namespace like 'domestic_patent', 'incoming_trademark', etc.
    """
    if not matter:
        return None

    overview = VMatterOverview.query.get(matter.matter_id)
    division, case_type = _infer_case_kind(matter, overview)
    try:
        profile = CaseParameterService.get_case_profile(division, case_type)
        if (profile.namespace or "").strip():
            return profile.namespace
    except Exception:
        current_app.logger.warning(
            "get_namespace_for_matter: failed to resolve profile (matter_id=%s, division=%s, type=%s)",
            str(getattr(matter, "matter_id", "") or ""),
            division,
            case_type,
            exc_info=True,
        )

    # Fallback: only accept a unique custom-field namespace (avoid ambiguity).
    rows = (
        MatterCustomField.query.filter_by(matter_id=matter.matter_id)
        .with_entities(MatterCustomField.namespace)
        .all()
    )
    collected: list[str] = []
    for row in rows:
        try:
            ns = row.namespace
        except Exception:
            ns = row[0] if row else None
        if ns and ns != "basic":
            collected.append(ns)
    namespaces = sorted(set(collected))
    if len(namespaces) == 1:
        return namespaces[0]
    if len(namespaces) > 1:
        current_app.logger.warning(
            "get_namespace_for_matter: multiple custom-field namespaces exist for matter_id=%s: %s",
            str(matter.matter_id),
            ",".join(namespaces),
        )

    return None


def get_canonical_value(matter_id: str, canonical_key: str) -> Optional[str]:
    """
    Get the value of a canonical field for a specific matter.
    Searches through applicable namespaces based on the matter type.
    """
    matter = Matter.query.get(matter_id)
    if not matter:
        return None

    config = load_canonical_config()
    targets = config.get(canonical_key, [])

    if not targets:
        return None

    # Get the primary namespace for this matter
    namespace = get_namespace_for_matter(matter)

    # Find matching target for this namespace
    for target in targets:
        target_namespace = target.get("namespace", "")
        if target_namespace == namespace or target_namespace == "basic":
            param_key = target.get("param_key", "")
            if not param_key:
                continue

            # Look up value in matter_custom_field
            custom_field = MatterCustomField.query.filter_by(
                matter_id=matter_id, namespace=target_namespace
            ).first()

            if custom_field and custom_field.data:
                value = custom_field.data.get(param_key)
                if value:
                    return str(value)

    return None


def compute_case_flat_index(matter_id: str) -> dict[str, Any]:
    """
    Compute all canonical field values for a matter.
    Returns dict ready for CaseFlatIndex upsert.
    """
    matter = Matter.query.get(matter_id)
    if not matter:
        return {}

    namespace = get_namespace_for_matter(matter)

    result: dict[str, Any] = {
        "matter_id": matter_id,
        "namespace": namespace,
        # NOTE: CaseFlatIndex.updated_at is DateTime(default/onupdate). Do NOT write a string here.
    }

    # Get all custom fields for this matter
    custom_fields = MatterCustomField.query.filter_by(matter_id=matter_id).all()
    data_by_namespace = {cf.namespace: cf.data or {} for cf in custom_fields}

    config = load_canonical_config()

    # Resolve each indexed canonical field
    for canonical_key in INDEXED_CANONICAL_FIELDS:
        targets = config.get(canonical_key, [])
        value = None

        # Try to find value from targets, prioritizing the primary namespace
        for target in targets:
            target_namespace = target.get("namespace", "")
            param_key = target.get("param_key", "")

            if not param_key:
                continue

            # Check if this namespace matches or is "basic"
            if target_namespace == namespace or target_namespace == "basic":
                data = data_by_namespace.get(target_namespace, {})
                if param_key in data and data[param_key]:
                    value = str(data[param_key])
                    break

        # Map to flat index column
        col_name = canonical_key
        result[col_name] = value

    # Once an official application document has supplied the application-form
    # applicant, prefer it over pre-filing/intake applicant fields for search
    # and list indexes.
    for ns in [namespace] + sorted(data_by_namespace.keys()):
        payload = data_by_namespace.get(ns) or {}
        if not isinstance(payload, dict):
            continue
        official_applicant = str(payload.get("application_applicant_name") or "").strip()
        if official_applicant:
            result["applicant"] = official_applicant
            break

    # Handle special fields with IDs (attorney_id, manager_id, handler_id)
    for staff_field in ["attorney", "manager", "handler"]:
        val = result.get(staff_field)
        id_key = f"{staff_field}_id"
        result[id_key] = _resolve_user_id(val) if val else None

    # Build search_text for compact search (combine view + flat index fields)
    overview = VMatterOverview.query.get(matter_id)
    identifier_values = [
        str(mi.id_value)
        for mi in MatterIdentifier.query.filter_by(matter_id=matter_id).all()
        if mi.id_value
    ]
    extra_search_keys = {
        "client_name",
        "applicant_name",
        "application_applicant_name",
        "applicant_registrant",
    }
    extra_search_values: list[str] = []
    for data in data_by_namespace.values():
        if not isinstance(data, dict):
            continue
        for key in extra_search_keys:
            raw = data.get(key)
            if not raw:
                continue
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    if item:
                        extra_search_values.append(str(item))
            else:
                extra_search_values.append(str(raw))
    search_parts = (
        [
            # Core refs
            (overview.our_ref if overview else "") or "",
            (overview.old_our_ref if overview else "") or "",
            (overview.your_ref if overview else "") or "",
            # Status
            (overview.status_red if overview else "") or "",
            (overview.status_blue if overview else "") or "",
            # Client / Title
            (overview.clients if overview else "") or "",
            (overview.right_name if overview else "") or "",
            (overview.applicants if overview else "") or "",
            (overview.attorneys if overview else "") or "",
            # Canonical fields from flat index
            result.get("attorney") or "",
            result.get("manager") or "",
            result.get("handler") or "",
            result.get("applicant") or "",
            result.get("client_name") or "",
            result.get("inventor") or "",
            result.get("application_no") or "",
            result.get("application_date") or "",
        ]
        + identifier_values
        + extra_search_values
    )
    search_text = " ".join(dict.fromkeys(p for p in search_parts if p))
    result["search_text"] = search_text
    # Compact form improves  search across whitespace boundaries.
    result["search_compact"] = to_compact_compact(search_text)

    return result


def _resolve_user_id(raw_name: str) -> Optional[int]:
    """Resolve a staff name string to a User ID."""

    def _coerce_user_id(user: User | None) -> Optional[int]:
        if not user:
            return None
        try:
            user_id = int(getattr(user, "id", 0) or 0)
        except (TypeError, ValueError):
            return None
        return user_id or None

    token = (raw_name or "").strip()
    if not token:
        return None

    # 1. Try "Name(ID)" pattern or "(ID)" or just digits
    # E.g. "Hong Gil Dong(admin)" -> we want to find user with username 'admin'

    # Check for direct integer ID (unlikely in name field but possible)
    if token.isdigit():
        u = User.query.filter_by(id=int(token), is_active=True).first()
        user_id = _coerce_user_id(u)
        if user_id is not None:
            return user_id

    # Check for username in parentheses
    match = re.search(r"\(([^)]+)\)", token)
    if match:
        user_param = match.group(1).strip()
        # Try username match
        u = User.query.filter(User.username == user_param, User.is_active.is_(True)).first()
        user_id = _coerce_user_id(u)
        if user_id is not None:
            return user_id
        # Try email match
        u = User.query.filter(User.email == user_param, User.is_active.is_(True)).first()
        user_id = _coerce_user_id(u)
        if user_id is not None:
            return user_id

    # 2. Try exact username match
    u = User.query.filter(User.username == token, User.is_active.is_(True)).first()
    user_id = _coerce_user_id(u)
    if user_id is not None:
        return user_id

    # 3. Try display name match
    u = User.query.filter(User.display_name == token, User.is_active.is_(True)).first()
    user_id = _coerce_user_id(u)
    if user_id is not None:
        return user_id

    # 4. Try email match
    if "@" in token:
        u = User.query.filter(User.email == token, User.is_active.is_(True)).first()
        user_id = _coerce_user_id(u)
        if user_id is not None:
            return user_id

    return None


def upsert_case_flat_index(matter_id: str) -> Optional[CaseFlatIndex]:
    """
    Compute and upsert case_flat_index for a single matter.
    Called after case save/update.
    Returns the CaseFlatIndex record or None if matter not found.
    """
    data = compute_case_flat_index(matter_id)
    if not data:
        return None

    # Upsert using ORM (PostgreSQL compatible)
    existing = CaseFlatIndex.query.get(matter_id)

    try:
        # NOTE:
        # - In production (PostgreSQL), we use SAVEPOINT isolation to avoid poisoning caller transactions.
        # - In tests (SQLite), nested SAVEPOINT commits can interact poorly with Session after_commit hooks
        #   and background-task fallbacks, producing "no such savepoint" errors. In SQLite, flush-only is
        #   sufficient and avoids those flaky interactions.
        dialect = ""
        try:
            dialect = (db.engine.dialect.name or "").lower()
        except Exception:
            dialect = ""

        if dialect == "sqlite":
            if existing:
                for key, value in data.items():
                    if key != "matter_id" and hasattr(existing, key):
                        setattr(existing, key, value)
                existing.updated_at = datetime.utcnow()
            else:
                existing = CaseFlatIndex(**data)
                db.session.add(existing)
            db.session.flush()
            return existing

        with db.session.begin_nested():
            if existing:
                # Update existing record
                for key, value in data.items():
                    if key != "matter_id" and hasattr(existing, key):
                        setattr(existing, key, value)
                existing.updated_at = datetime.utcnow()
            else:
                # Create new record
                existing = CaseFlatIndex(**data)
                db.session.add(existing)
            db.session.flush()
        return existing
    except Exception as e:
        current_app.logger.error(f"Failed to upsert case_flat_index for {matter_id}: {e}")
        raise


def delete_case_flat_index(matter_id: str) -> bool:
    """
    Delete case_flat_index for a matter (used on cascade delete).
    """
    try:
        dialect = ""
        try:
            dialect = (db.engine.dialect.name or "").lower()
        except Exception:
            dialect = ""

        if dialect == "sqlite":
            CaseFlatIndex.query.filter_by(matter_id=matter_id).delete()
            db.session.flush()
            return True

        with db.session.begin_nested():
            CaseFlatIndex.query.filter_by(matter_id=matter_id).delete()
            db.session.flush()
        return True
    except Exception as e:
        current_app.logger.error(f"Failed to delete case_flat_index for {matter_id}: {e}")
        return False


def backfill_all_case_flat_index(batch_size: int = 100, limit: int | None = None) -> dict[str, int]:
    """
    Backfill case_flat_index for all existing matters.
    Returns stats dict with counts.
    """
    stats = {"processed": 0, "success": 0, "failed": 0}

    query = Matter.query
    if limit:
        query = query.limit(limit)

    matters = query.all()

    for i, matter in enumerate(matters):
        try:
            result = upsert_case_flat_index(matter.matter_id)
            if result:
                stats["success"] += 1
            else:
                stats["failed"] += 1
        except Exception as e:
            current_app.logger.error(f"Backfill failed for {matter.matter_id}: {e}")
            stats["failed"] += 1

        stats["processed"] += 1

        # Keep service helpers flush-only; transaction boundaries belong to callers.
        if (i + 1) % batch_size == 0:
            db.session.flush()

    db.session.flush()

    return stats
