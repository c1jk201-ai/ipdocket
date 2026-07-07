from __future__ import annotations

import json
import re

from sqlalchemy import String, cast, func, or_

from app.extensions import db
from app.models.client import Client
from app.models.ip_records import MatterCustomField, VMatterOverview
from app.utils.error_logging import report_swallowed_exception

APPLICANT_CODE_LIMIT = 3
_APPLICANT_CODE_SPLIT_RE = re.compile(r"[;,\n/]+")
_CRM_CLIENT_MATTER_NAMESPACES = (
    "domestic_patent",
    "domestic_design",
    "domestic_trademark",
    "incoming_patent",
    "incoming_design",
    "incoming_trademark",
    "outgoing_patent",
    "outgoing_design",
    "outgoing_trademark",
    "pct",
    "litigation",
    "misc",
    "basic",
)


def normalize_applicant_code(value: str | None) -> str:
    cleaned = re.sub(r"\s+", "", value or "").strip()
    return cleaned.upper() if cleaned else ""


def split_applicant_codes(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    text = _APPLICANT_CODE_SPLIT_RE.sub(";", text)
    tokens: list[str] = []
    for chunk in text.split(";"):
        for part in chunk.split():
            part = part.strip()
            if part:
                tokens.append(part)
    return tokens


def _collapse_spaces(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _client_name_variants(name: str | None) -> list[str]:
    base = _collapse_spaces(name)
    if not base:
        return []

    base_no_paren = re.sub(r"\s*\([^)]*\)\s*", " ", base).strip()
    prefixes = ("Company ", "Company ", "()", "㈜", "()")

    def strip_prefix(s: str) -> str:
        for pref in prefixes:
            if s.startswith(pref):
                return s[len(pref) :].strip()
        return s

    core = strip_prefix(base)
    core_no_paren = strip_prefix(base_no_paren) if base_no_paren else ""
    variants = {base, core}
    if base_no_paren:
        variants.add(base_no_paren)
    if core_no_paren:
        variants.add(core_no_paren)
    if core:
        variants.add(f"Company {core}")
        variants.add(f"(){core}")
        variants.add(f"㈜{core}")
    return [v for v in variants if v]


def extract_client_applicant_codes(client: Client) -> list[str]:
    extra = client.extra if isinstance(client.extra, dict) else {}
    codes = extra.get("applicant_codes", [])
    if isinstance(codes, str):
        codes = split_applicant_codes(codes)
    elif not isinstance(codes, (list, tuple, set)):
        codes = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for code in codes:
        norm = normalize_applicant_code(str(code))
        if not norm or norm in seen:
            continue
        cleaned.append(norm)
        seen.add(norm)
    return cleaned


def _collect_applicant_codes_from_matters(matter_ids: list[str]) -> list[str]:
    if not matter_ids:
        return []

    rows = (
        MatterCustomField.query.with_entities(MatterCustomField.data)
        .filter(MatterCustomField.matter_id.in_(matter_ids))
        .all()
    )

    candidates: list[str] = []
    for (data,) in rows:
        payload = data
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            continue
        raw = payload.get("application_applicant_customer_no")
        if not raw:
            continue
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                candidates.extend(split_applicant_codes(str(item)))
        else:
            candidates.extend(split_applicant_codes(str(raw)))

    cleaned: list[str] = []
    seen: set[str] = set()
    for code in candidates:
        norm = normalize_applicant_code(code)
        if not norm or norm in seen:
            continue
        cleaned.append(norm)
        seen.add(norm)
    return cleaned


def _collect_applicant_codes_by_applicant_names(names: list[str]) -> list[str]:
    if not names:
        return []

    try:
        bind = db.session.get_bind()
        dialect = (getattr(bind.dialect, "name", "") or "").lower() if bind else ""
    except Exception:
        dialect = ""

    if dialect.startswith("postgres"):
        app_form_expr = MatterCustomField.data["application_applicant_name"].as_string()
        applicant_expr = MatterCustomField.data["applicant_name"].as_string()
    else:
        app_form_expr = func.json_extract(MatterCustomField.data, "$.application_applicant_name")
        applicant_expr = func.json_extract(MatterCustomField.data, "$.applicant_name")
    app_form_expr = cast(app_form_expr, String)
    applicant_expr = cast(applicant_expr, String)

    conditions = []
    for nm in names:
        nm = _collapse_spaces(nm)
        if not nm:
            continue
        like_val = f"%{nm.lower()}%"
        conditions.append(func.lower(app_form_expr).like(like_val))
        conditions.append(func.lower(applicant_expr).like(like_val))

    if not conditions:
        return []

    rows = (
        MatterCustomField.query.with_entities(MatterCustomField.matter_id, MatterCustomField.data)
        .filter(MatterCustomField.namespace.in_(_CRM_CLIENT_MATTER_NAMESPACES))
        .filter(or_(*conditions))
        .limit(200)
        .all()
    )

    candidates: list[str] = []
    matched_matter_ids: set[str] = set()
    for mid, data in rows:
        if mid:
            matched_matter_ids.add(str(mid))
        payload = data
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            continue
        raw = payload.get("application_applicant_customer_no")
        if not raw:
            continue
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                candidates.extend(split_applicant_codes(str(item)))
        else:
            candidates.extend(split_applicant_codes(str(raw)))

    try:
        ov_conditions = []
        for nm in names:
            nm = _collapse_spaces(nm)
            if not nm:
                continue
            ov_conditions.append(func.lower(VMatterOverview.applicants).like(f"%{nm.lower()}%"))
        if ov_conditions:
            ov_rows = (
                VMatterOverview.query.with_entities(VMatterOverview.matter_id)
                .filter(or_(*ov_conditions))
                .limit(200)
                .all()
            )
            ov_ids = {str(mid) for (mid,) in ov_rows if mid}
            extra_ids = [mid for mid in ov_ids if mid not in matched_matter_ids]
            if extra_ids:
                candidates.extend(_collect_applicant_codes_from_matters(extra_ids))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="crm.applicant_code_suggestion.overview_lookup",
            log_key="crm.applicant_code_suggestion.overview_lookup",
            log_window_seconds=300,
        )

    cleaned: list[str] = []
    seen: set[str] = set()
    for code in candidates:
        norm = normalize_applicant_code(code)
        if not norm or norm in seen:
            continue
        cleaned.append(norm)
        seen.add(norm)
    return cleaned


def build_applicant_code_suggestion(
    client: Client, matter_ids: list[str], *, debug: dict | None = None
) -> dict | None:
    existing_codes = extract_client_applicant_codes(client)
    slots = max(0, APPLICANT_CODE_LIMIT - len(existing_codes))
    if debug is not None:
        debug.update(
            {
                "client_id": getattr(client, "id", None),
                "client_name": (client.name or "").strip(),
                "existing_codes": existing_codes,
                "slots": slots,
                "matter_count": len(matter_ids),
                "matter_ids_preview": matter_ids[:5],
            }
        )
    if slots <= 0:
        if debug is not None:
            debug["reason"] = "no_slots"
        return None

    candidates = _collect_applicant_codes_from_matters(matter_ids)
    if debug is not None:
        debug["candidates_primary_count"] = len(candidates)
    if not candidates:
        name_variants = _client_name_variants(client.name)
        if debug is not None:
            debug["name_variants"] = name_variants
        candidates = _collect_applicant_codes_by_applicant_names(name_variants)
        if debug is not None:
            debug["candidates_name_match_count"] = len(candidates)
        if not candidates:
            if debug is not None:
                debug["reason"] = "no_candidates"
            return None

    existing_norm = {normalize_applicant_code(c) for c in existing_codes}
    missing = [c for c in candidates if normalize_applicant_code(c) not in existing_norm]
    if not missing:
        if debug is not None:
            debug["reason"] = "all_existing"
        return None

    suggestion = {
        "codes": missing[:slots],
        "slots": slots,
        "extra_count": max(0, len(missing) - slots),
    }
    if debug is not None:
        debug["suggestion"] = suggestion
    return suggestion
