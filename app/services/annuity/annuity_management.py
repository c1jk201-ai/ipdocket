from __future__ import annotations

from typing import Iterable

from sqlalchemy import String, cast, func, or_

from app.extensions import db
from app.models.case import Case
from app.models.client import Client
from app.models.ip_records import Matter, MatterCustomField, MatterPartyRole
from app.utils.error_logging import report_swallowed_exception

ANNUITY_MANAGEMENT_DISABLED_KEY = "annuity_management_disabled"

_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "f", ""}


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    try:
        text = str(value).strip().lower()
    except Exception:
        return bool(default)
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(default)


def is_annuity_management_disabled_for_client(client: Client | None) -> bool:
    if client is None:
        return False
    extra = getattr(client, "extra", None)
    if not isinstance(extra, dict):
        return False
    if ANNUITY_MANAGEMENT_DISABLED_KEY not in extra:
        return False
    return _coerce_bool(extra.get(ANNUITY_MANAGEMENT_DISABLED_KEY), False)


def _disabled_client_sets() -> tuple[set[str], set[str]]:
    client_ids: set[str] = set()
    party_ids: set[str] = set()
    try:
        clients = (
            Client.query.filter(or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)))
            .filter(Client.extra.isnot(None))
            .all()
        )
        for client in clients:
            if not is_annuity_management_disabled_for_client(client):
                continue
            cid = str(getattr(client, "id", "") or "").strip()
            if cid:
                client_ids.add(cid)
            for party_id in (
                getattr(client, "party_id", None),
                getattr(client, "ipm_party_id", None),
            ):
                pid = str(party_id or "").strip()
                if pid:
                    party_ids.add(pid)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_management.disabled_client_sets",
            log_key="annuity_management.disabled_client_sets",
            log_window_seconds=300,
        )
    return client_ids, party_ids


def _matter_custom_field_client_id_expr():
    try:
        bind = db.session.get_bind()
        dialect = (getattr(bind.dialect, "name", "") or "").lower() if bind else ""
    except Exception:
        dialect = ""

    if dialect.startswith("postgres"):
        expr = MatterCustomField.data["client_id"].as_string()
    else:
        expr = func.json_extract(MatterCustomField.data, "$.client_id")
    return cast(expr, String)


def resolve_annuity_management_disabled_matter_ids(
    matter_ids: Iterable[str] | None = None,
) -> set[str]:
    disabled_client_ids, disabled_party_ids = _disabled_client_sets()
    if not disabled_client_ids and not disabled_party_ids:
        return set()

    candidate_matter_ids = [
        str(mid or "").strip() for mid in (matter_ids or []) if str(mid or "").strip()
    ]
    candidate_filter = bool(candidate_matter_ids)
    resolved: set[str] = set()

    if disabled_client_ids:
        try:
            client_id_expr = _matter_custom_field_client_id_expr()
            q = (
                db.session.query(MatterCustomField.matter_id)
                .filter(func.nullif(func.trim(client_id_expr), "").isnot(None))
                .filter(client_id_expr.in_(list(disabled_client_ids)))
            )
            if candidate_filter:
                q = q.filter(MatterCustomField.matter_id.in_(candidate_matter_ids))
            resolved.update(str(mid) for (mid,) in q.distinct().all() if mid)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_management.resolve_disabled_matter_ids.custom_field",
                log_key="annuity_management.resolve_disabled_matter_ids.custom_field",
                log_window_seconds=300,
            )

    if disabled_party_ids:
        try:
            q = (
                db.session.query(MatterPartyRole.matter_id)
                .filter(func.lower(func.coalesce(MatterPartyRole.role_code, "")) == "client")
                .filter(MatterPartyRole.party_id.in_(list(disabled_party_ids)))
            )
            if candidate_filter:
                q = q.filter(MatterPartyRole.matter_id.in_(candidate_matter_ids))
            resolved.update(str(mid) for (mid,) in q.distinct().all() if mid)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_management.resolve_disabled_matter_ids.party_role",
                log_key="annuity_management.resolve_disabled_matter_ids.party_role",
                log_window_seconds=300,
            )

    if disabled_client_ids:
        try:
            disabled_case_client_ids = [
                int(cid) for cid in disabled_client_ids if str(cid).isdigit()
            ]
            if disabled_case_client_ids:
                if candidate_filter:
                    matter_rows = (
                        Matter.query.with_entities(
                            Matter.matter_id,
                            Matter.our_ref,
                            Matter.old_our_ref,
                            Matter.your_ref,
                        )
                        .filter(Matter.matter_id.in_(candidate_matter_ids))
                        .all()
                    )
                    ref_to_mid: dict[str, str] = {}
                    candidate_refs: set[str] = set()
                    for mid, our_ref, old_our_ref, your_ref in matter_rows:
                        mid_str = str(mid or "").strip()
                        if not mid_str:
                            continue
                        for ref in (our_ref, old_our_ref, your_ref):
                            ref_str = str(ref or "").strip()
                            if not ref_str:
                                continue
                            candidate_refs.add(ref_str)
                            ref_to_mid.setdefault(ref_str, mid_str)
                    if candidate_refs:
                        case_rows = (
                            Case.query.with_entities(Case.ref_no)
                            .filter(Case.client_id.in_(disabled_case_client_ids))
                            .filter(or_(Case.is_deleted.is_(False), Case.is_deleted.is_(None)))
                            .filter(Case.ref_no.in_(list(candidate_refs)))
                            .all()
                        )
                        for (ref_no,) in case_rows:
                            mid = ref_to_mid.get(str(ref_no or "").strip())
                            if mid:
                                resolved.add(mid)
                else:
                    case_refs = {
                        str(ref_no or "").strip()
                        for (ref_no,) in (
                            Case.query.with_entities(Case.ref_no)
                            .filter(Case.client_id.in_(disabled_case_client_ids))
                            .filter(or_(Case.is_deleted.is_(False), Case.is_deleted.is_(None)))
                            .all()
                        )
                        if str(ref_no or "").strip()
                    }
                    if case_refs:
                        q = Matter.query.with_entities(Matter.matter_id).filter(
                            or_(
                                Matter.our_ref.in_(list(case_refs)),
                                Matter.old_our_ref.in_(list(case_refs)),
                                Matter.your_ref.in_(list(case_refs)),
                            )
                        )
                        resolved.update(str(mid) for (mid,) in q.distinct().all() if mid)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_management.resolve_disabled_matter_ids.legacy_case",
                log_key="annuity_management.resolve_disabled_matter_ids.legacy_case",
                log_window_seconds=300,
            )

    if candidate_filter:
        resolved &= set(candidate_matter_ids)
    return resolved


def is_annuity_management_disabled_for_matter(matter_id: str | None) -> bool:
    mid = str(matter_id or "").strip()
    if not mid:
        return False
    return mid in resolve_annuity_management_disabled_matter_ids([mid])
