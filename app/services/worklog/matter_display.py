from __future__ import annotations

import re

from sqlalchemy import func, or_

from app.extensions import db
from app.models.client import Client
from app.models.ip_records import MatterCustomField, MatterPartyRole
from app.utils.error_logging import report_swallowed_exception


def _merge_party_names(values: list[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for token in re.split(r"[;,]", str(raw or "")):
            item = token.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
    return "; ".join(out)


def _empty_worklog_matter_display_context() -> dict[str, object]:
    return {
        "matter_applicant_map": {},
        "matter_applicant_client_id_map": {},
        "matter_attorney_map": {},
        "matter_handler_map": {},
        "matter_manager_map": {},
        "matter_staff_map": {},
    }


def load_worklog_matter_display_context(matter_ids: set[str] | list[str]) -> dict[str, object]:
    normalized_ids = {
        str(mid or "").strip() for mid in (matter_ids or []) if str(mid or "").strip()
    }
    context = _empty_worklog_matter_display_context()
    matter_applicant_map = context["matter_applicant_map"]
    matter_applicant_client_id_map = context["matter_applicant_client_id_map"]
    matter_attorney_map = context["matter_attorney_map"]
    matter_handler_map = context["matter_handler_map"]
    matter_manager_map = context["matter_manager_map"]
    matter_staff_map = context["matter_staff_map"]

    if not normalized_ids:
        return context

    try:
        from app.models.party import Party

        applicant_rows = (
            db.session.query(
                MatterPartyRole.matter_id,
                MatterPartyRole.party_id,
                func.coalesce(Party.name_display, MatterPartyRole.raw_text, ""),
            )
            .outerjoin(Party, Party.party_id == MatterPartyRole.party_id)
            .filter(MatterPartyRole.matter_id.in_(sorted(normalized_ids)))
            .filter(func.lower(func.coalesce(MatterPartyRole.role_code, "")) == "applicant")
            .order_by(
                MatterPartyRole.matter_id.asc(),
                func.coalesce(MatterPartyRole.seq, 0).asc(),
                MatterPartyRole.mpr_id.asc(),
            )
            .all()
        )
        applicants_by_mid: dict[str, list[str]] = {}
        applicant_party_ids_by_mid: dict[str, set[str]] = {}
        for mid, party_id, name in applicant_rows:
            matter_id = str(mid or "").strip()
            display_name = str(name or "").strip()
            if not (matter_id and display_name):
                continue
            applicants_by_mid.setdefault(matter_id, []).append(display_name)
            party_token = str(party_id or "").strip()
            if party_token:
                applicant_party_ids_by_mid.setdefault(matter_id, set()).add(party_token)

        for matter_id, names in applicants_by_mid.items():
            merged = _merge_party_names(names)
            if merged:
                matter_applicant_map[matter_id] = merged

        all_party_ids = sorted(
            {
                pid
                for party_ids in applicant_party_ids_by_mid.values()
                for pid in party_ids
                if str(pid or "").strip()
            }
        )
        if all_party_ids:
            client_rows = (
                Client.query.with_entities(Client.id, Client.party_id, Client.ipm_party_id)
                .filter(or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)))
                .filter(
                    or_(
                        Client.party_id.in_(all_party_ids),
                        Client.ipm_party_id.in_(all_party_ids),
                    )
                )
                .all()
            )
            client_id_by_party_id: dict[str, int] = {}
            for client_id, party_id_1, party_id_2 in client_rows:
                for party_id in (party_id_1, party_id_2):
                    token = str(party_id or "").strip()
                    if token:
                        client_id_by_party_id[token] = int(client_id)

            for matter_id, party_ids in applicant_party_ids_by_mid.items():
                resolved = {
                    client_id_by_party_id.get(str(pid).strip())
                    for pid in party_ids
                    if client_id_by_party_id.get(str(pid).strip())
                }
                resolved = {int(cid) for cid in resolved if cid}
                if len(resolved) == 1:
                    matter_applicant_client_id_map[matter_id] = str(next(iter(resolved)))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="worklog.matter_display.case_applicants.party_roles",
            log_key="worklog.matter_display.case_applicants.party_roles",
            log_window_seconds=300,
        )

    missing_applicant_ids = [mid for mid in normalized_ids if not matter_applicant_map.get(mid)]
    if missing_applicant_ids:
        try:
            custom_rows = (
                MatterCustomField.query.with_entities(
                    MatterCustomField.matter_id,
                    MatterCustomField.namespace,
                    MatterCustomField.data,
                )
                .filter(MatterCustomField.matter_id.in_(sorted(missing_applicant_ids)))
                .all()
            )
            for mid, _namespace, data in custom_rows:
                matter_id = str(mid or "").strip()
                if (
                    not matter_id
                    or matter_applicant_map.get(matter_id)
                    or not isinstance(data, dict)
                ):
                    continue
                raw = (
                    str(data.get("application_applicant_name") or "").strip()
                    or str(data.get("applicant_name") or "").strip()
                )
                if raw:
                    matter_applicant_map[matter_id] = raw
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="worklog.matter_display.case_applicants.custom_fields",
                log_key="worklog.matter_display.case_applicants.custom_fields",
                log_window_seconds=300,
            )

    try:
        custom_fields = MatterCustomField.query.filter(
            MatterCustomField.matter_id.in_(normalized_ids), MatterCustomField.namespace == "basic"
        ).all()
        for custom_field in custom_fields:
            if not custom_field.data:
                continue
            if custom_field.data.get("attorney"):
                matter_attorney_map[custom_field.matter_id] = custom_field.data.get("attorney")
            if custom_field.data.get("handler"):
                matter_handler_map[custom_field.matter_id] = custom_field.data.get("handler")
            if custom_field.data.get("manager"):
                matter_manager_map[custom_field.matter_id] = custom_field.data.get("manager")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="worklog.matter_display.staff_custom_fields",
            log_key="worklog.matter_display.staff_custom_fields",
            log_window_seconds=300,
        )

    try:
        from app.models.party import Party, PartyStaff
        from app.models.ip_records import MatterStaffAssignment

        role_expr = func.lower(func.trim(MatterStaffAssignment.staff_role_code))
        msa_rows = (
            db.session.query(
                MatterStaffAssignment.matter_id,
                MatterStaffAssignment.staff_role_code,
                MatterStaffAssignment.staff_party_id,
                Party.name_display,
            )
            .join(PartyStaff, PartyStaff.party_id == MatterStaffAssignment.staff_party_id)
            .join(Party, Party.party_id == PartyStaff.party_id)
            .filter(MatterStaffAssignment.matter_id.in_(sorted(normalized_ids)))
            .filter(
                role_expr.in_(
                    (
                        "attorney",
                        "retainer",
                        "handler",
                        "staff",
                        "draftsman",
                        "manager",
                        "mgmt",
                    )
                )
            )
            .all()
        )

        for matter_id, role, staff_party_id, name in msa_rows:
            if matter_id not in matter_staff_map:
                matter_staff_map[matter_id] = {"attorney": [], "handler": [], "manager": []}

            role_code = (role or "").strip().lower()
            display_name = (name or "").strip()
            if not display_name:
                continue
            row_id = (str(staff_party_id) or "").strip()
            if not row_id:
                continue
            staff_row = {"id": row_id, "name": display_name}

            if role_code in ("attorney", "retainer"):
                if row_id not in {p.get("id") for p in matter_staff_map[matter_id]["attorney"]}:
                    matter_staff_map[matter_id]["attorney"].append(staff_row)
            elif role_code in ("handler", "staff", "draftsman"):
                if row_id not in {p.get("id") for p in matter_staff_map[matter_id]["handler"]}:
                    matter_staff_map[matter_id]["handler"].append(staff_row)
            elif role_code in ("manager", "mgmt"):
                if row_id not in {p.get("id") for p in matter_staff_map[matter_id]["manager"]}:
                    matter_staff_map[matter_id]["manager"].append(staff_row)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="worklog.matter_display.staff_assignments",
            log_key="worklog.matter_display.staff_assignments",
            log_window_seconds=300,
        )

    return context
