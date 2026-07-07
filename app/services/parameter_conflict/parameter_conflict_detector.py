from typing import Any, Callable

from app.services.parameter_conflict.parameter_conflict_types import (
    ConflictItem,
    ParameterExtractionResult,
    _normalize_identifier,
    _parse_date_str,
)
from app.utils.error_logging import report_swallowed_exception


def _is_hidden_field(*, field_definitions: dict, field_name: str) -> bool:
    try:
        return bool((field_definitions.get(field_name) or {}).get("hidden"))
    except Exception:
        return False


def _maybe_append_visible(
    *,
    bucket: list[ConflictItem],
    item: ConflictItem,
    field_definitions: dict,
    allow_hidden: bool = False,
) -> None:
    is_hidden = item.hidden or _is_hidden_field(
        field_definitions=field_definitions,
        field_name=item.field_name,
    )
    if is_hidden:
        item.hidden = True
        if not allow_hidden:
            return
    bucket.append(item)


def _create_conflict_item(
    *,
    field_definitions: dict,
    field_name: str,
    current: Any,
    new_val: Any,
) -> ConflictItem:
    field_def = field_definitions.get(field_name, {})
    return ConflictItem(
        field_name=field_name,
        field_label=field_def.get("label", field_name),
        current_value=str(current) if current is not None else None,
        new_value=str(new_val) if new_val is not None else None,
        table_name=field_def.get("table", "matter"),
        field_key=field_name,
        priority=field_def.get("priority", 2),
        hidden=bool(field_def.get("hidden")),
    )


_CUSTOM_FIELD_PARAM_NAMES = (
    "title",
    "title_en",
    "application_agent",
    "application_applicant_name",
    "application_applicant_customer_no",
    "pct_application_no",
    "pct_application_date",
    "filing_type",
    "right_type",
    "tm_name",
    "tm_type",
    "application_classes",
    "application_goods",
    "article_name",
    "is_partial",
    "registrant_name",
    "priority_exam_request",
    "expedited_request_date",
)


def _is_incoming_pct_national_phase_extraction(
    *,
    namespace: str,
    extracted_params: dict,
) -> bool:
    ns = (namespace or "").strip().lower()
    if not ns.startswith("incoming_") or not any(token in ns for token in ("patent", "utility")):
        return False
    if str(extracted_params.get("pct_application_no") or "").strip():
        return True
    if str(extracted_params.get("pct_application_date") or "").strip():
        return True

    signal = " ".join(
        str(extracted_params.get(key) or "")
        for key in ("doc_type", "filing_type", "app_type", "application_type")
    )
    compact = signal.replace(" ", "").upper()
    return "PCT" in compact or "Filing" in signal


def _route_detected_item(
    *,
    item: ConflictItem,
    new_val: Any,
    auto_apply: list[ConflictItem],
    conflicts: list[ConflictItem],
    field_definitions: dict,
    allow_hidden_auto_apply: bool = True,
) -> None:
    if item.has_conflict:
        _maybe_append_visible(bucket=conflicts, item=item, field_definitions=field_definitions)
    elif new_val:
        _maybe_append_visible(
            bucket=auto_apply,
            item=item,
            field_definitions=field_definitions,
            allow_hidden=allow_hidden_auto_apply,
        )


def _route_plain_item(
    *,
    item: ConflictItem,
    new_val: Any,
    auto_apply: list[ConflictItem],
    conflicts: list[ConflictItem],
) -> None:
    if item.has_conflict:
        conflicts.append(item)
    elif new_val:
        auto_apply.append(item)


def _detect_field_value(
    *,
    field_definitions: dict,
    field_name: str,
    current: Any,
    new_val: Any,
    auto_apply: list[ConflictItem],
    conflicts: list[ConflictItem],
) -> ConflictItem:
    item = _create_conflict_item(
        field_definitions=field_definitions,
        field_name=field_name,
        current=current,
        new_val=new_val,
    )
    _route_detected_item(
        item=item,
        new_val=new_val,
        auto_apply=auto_apply,
        conflicts=conflicts,
        field_definitions=field_definitions,
    )
    return item


def detect_conflicts(
    *,
    matter_id: str,
    matter_data: dict,
    extracted_params: dict,
    field_definitions: dict,
    get_custom_field_namespace: Callable[[], str],
) -> ParameterExtractionResult:
    auto_apply: list[ConflictItem] = []
    conflicts: list[ConflictItem] = []
    skipped: list[str] = []

    our_ref = matter_data.get("our_ref", "")

    if "right_name" in extracted_params:
        item = _detect_field_value(
            field_name="right_name",
            current=matter_data.get("right_name"),
            new_val=extracted_params.get("right_name"),
            field_definitions=field_definitions,
            auto_apply=auto_apply,
            conflicts=conflicts,
        )
        item.field_key = "right_name"

    custom_fields = matter_data.get("custom_fields", {})
    for field_name in _CUSTOM_FIELD_PARAM_NAMES:
        if field_name not in extracted_params:
            skipped.append(field_name)
            continue
        _detect_field_value(
            field_definitions=field_definitions,
            field_name=field_name,
            current=custom_fields.get(field_name),
            new_val=extracted_params[field_name],
            auto_apply=auto_apply,
            conflicts=conflicts,
        )

    if "identifiers" in extracted_params:
        id_type_translation = {
            "APP_NO": "Application No.",
            "REG_NO": "Registration No.",
            "PUB_NO": "Publication No.",
        }

        for id_info in extracted_params["identifiers"]:
            id_type_raw = id_info["id_type"]
            new_val = id_info["id_value"]

            id_type_display = id_type_translation.get(id_type_raw, id_type_raw)

            current = matter_data.get("identifiers", {}).get(id_type_raw)
            if current is None:
                current = matter_data.get("identifiers", {}).get(id_type_display)
            if isinstance(current, list):
                current = current[0] if current else None

            field_name = f"identifier_{id_type_display}"
            item = ConflictItem(
                field_name=field_name,
                field_label=f" ({id_type_display})",
                current_value=current,
                new_value=new_val,
                table_name="matter_identifier",
                field_key=id_type_raw,
                priority=1,
            )
            _route_plain_item(
                item=item,
                new_val=new_val,
                auto_apply=auto_apply,
                conflicts=conflicts,
            )

            if id_type_raw == "APP_NO" and new_val:
                _detect_field_value(
                    field_name="application_no",
                    current=matter_data.get("custom_fields", {}).get("application_no"),
                    new_val=new_val,
                    field_definitions=field_definitions,
                    auto_apply=auto_apply,
                    conflicts=conflicts,
                )

    if "app_no" in extracted_params and extracted_params["app_no"]:
        app_no_val = extracted_params["app_no"]
        already_added = any(
            c.field_name == "identifier_Application No." or c.field_name == "identifier_APP_NO"
            for c in conflicts + auto_apply
        )
        if not already_added:
            current_id = matter_data.get("identifiers", {}).get("APP_NO")
            if current_id is None:
                current_id = matter_data.get("identifiers", {}).get("Application No.")

            item = ConflictItem(
                field_name="identifier_Application No.",
                field_label="Application No.",
                current_value=current_id,
                new_value=app_no_val,
                table_name="matter_identifier",
                field_key="APP_NO",
                priority=1,
            )
            _route_plain_item(
                item=item,
                new_val=app_no_val,
                auto_apply=auto_apply,
                conflicts=conflicts,
            )

            _detect_field_value(
                field_name="application_no",
                current=matter_data.get("custom_fields", {}).get("application_no"),
                new_val=app_no_val,
                field_definitions=field_definitions,
                auto_apply=auto_apply,
                conflicts=conflicts,
            )

    app_date_val = None
    exam_requested_val = None
    exam_request_date_val = None
    force_exam_request_date = False

    if "events" in extracted_params:
        for event in extracted_params["events"]:
            event_key = event["event_key"]

            if event_key == "CLAIM_COUNT":
                claim_count_val = event.get("raw_text") or event.get("event_at") or ""
                if claim_count_val:
                    current_cf = matter_data.get("custom_fields", {}).get("claims_total")
                    item_cf = _create_conflict_item(
                        field_definitions=field_definitions,
                        field_name="claims_total",
                        current=current_cf,
                        new_val=claim_count_val,
                    )
                    _route_plain_item(
                        item=item_cf,
                        new_val=claim_count_val,
                        auto_apply=auto_apply,
                        conflicts=conflicts,
                    )
                continue

            if event_key == "EXAM_REQ":
                new_val = event.get("raw_text") or event.get("event_at")
            else:
                new_val = event.get("event_at") or event.get("raw_text")
            current = matter_data.get("events", {}).get(event_key)

            label = {
                "APP_DATE": "Filing date",
                "APP_TYPE": "Filing type",
                "EXAM_REQ": "Examination request",
                "CLAIM_COUNT": "Billing ",
                "PUB_DATE": "Publication date",
                "REG_DATE": "Registration date",
            }.get(event_key, event_key)

            item = ConflictItem(
                field_name=f"event_{event_key}",
                field_label=label,
                current_value=current,
                new_value=new_val,
                table_name="matter_event",
                field_key=event_key,
                priority=2,
            )

            _route_plain_item(
                item=item,
                new_val=new_val,
                auto_apply=auto_apply,
                conflicts=conflicts,
            )

            if event_key == "APP_DATE":
                app_date_val = event.get("event_at")
                if app_date_val:
                    _detect_field_value(
                        field_name="application_date",
                        current=matter_data.get("custom_fields", {}).get("application_date"),
                        new_val=app_date_val,
                        field_definitions=field_definitions,
                        auto_apply=auto_apply,
                        conflicts=conflicts,
                    )

                priority_dates = []
                priority_claim_pairs: list[tuple[object, str]] = []

                if app_date_val:
                    ad = _parse_date_str(app_date_val)
                    if ad:
                        priority_dates.append(ad)

                if "priority_claims" in extracted_params:
                    for p_claim in extracted_params["priority_claims"]:
                        number = p_claim.get("number")
                        pd_str = p_claim.get("date")
                        if pd_str:
                            pd = _parse_date_str(pd_str)
                            if pd:
                                priority_dates.append(pd)
                                if number:
                                    priority_claim_pairs.append((pd, str(number).strip()))

                    existing_priorities = matter_data.get("identifiers", {}).get(
                        "Priority", []
                    )
                    if not isinstance(existing_priorities, list):
                        existing_priorities = [existing_priorities] if existing_priorities else []

                    added_in_session = set()

                    for i, p_claim in enumerate(extracted_params["priority_claims"]):
                        country = p_claim.get("country")
                        number = p_claim.get("number")
                        if not number:
                            continue

                        norm_num = _normalize_identifier(number)

                        is_exist = False
                        for ex in existing_priorities:
                            if _normalize_identifier(ex) == norm_num:
                                is_exist = True
                                break

                        if not is_exist and norm_num in added_in_session:
                            is_exist = True

                        if not is_exist:
                            field_name = f"identifier_priority_{i}"
                            item_id = ConflictItem(
                                field_name=field_name,
                                field_label=f"Priority ({country or 'New'})",
                                current_value=None,
                                new_value=number,
                                table_name="matter_identifier",
                                field_key="Priority",
                                priority=2,
                            )
                            auto_apply.append(item_id)
                            added_in_session.add(norm_num)

                if priority_dates:
                    try:
                        from dateutil.relativedelta import relativedelta

                        earliest_priority = min(priority_dates)
                        earliest_priority_number = ""
                        if priority_claim_pairs:
                            earliest_priority_number = min(
                                priority_claim_pairs,
                                key=lambda item: (item[0], item[1]),
                            )[1]

                        ns = get_custom_field_namespace()
                        is_patent = "patent" in ns.lower()
                        skip_foreign_filing_due = _is_incoming_pct_national_phase_extraction(
                            namespace=ns,
                            extracted_params=extracted_params,
                        )

                        offset = relativedelta(years=1) if is_patent else relativedelta(months=6)
                        foreign_due = earliest_priority + offset
                        foreign_due_str = foreign_due.isoformat()

                        if not skip_foreign_filing_due:
                            current_ff = matter_data.get("events", {}).get(
                                "FOREIGN_FILING_DEADLINE"
                            )
                            item_ff = _create_conflict_item(
                                field_definitions=field_definitions,
                                field_name="foreign_filing_deadline",
                                current=current_ff,
                                new_val=foreign_due_str,
                            )
                            item_ff.field_key = "FOREIGN_FILING_DEADLINE"
                            item_ff.field_label = "ForeignFilingDeadline (Priority)"

                            _route_plain_item(
                                item=item_ff,
                                new_val=foreign_due_str,
                                auto_apply=auto_apply,
                                conflicts=conflicts,
                            )

                            current_ff_cf = matter_data.get("custom_fields", {}).get(
                                "foreign_filing_deadline"
                            )
                            item_ff_cf = ConflictItem(
                                field_name="custom_foreign_filing_deadline",
                                field_label="ForeignFilingDeadline",
                                current_value=current_ff_cf,
                                new_value=foreign_due_str,
                                table_name="matter_custom_field",
                                field_key="foreign_filing_deadline",
                                priority=1,
                            )
                            _route_plain_item(
                                item=item_ff_cf,
                                new_val=foreign_due_str,
                                auto_apply=auto_apply,
                                conflicts=conflicts,
                            )

                        earliest_priority_str = earliest_priority.isoformat()
                        current_ep = matter_data.get("events", {}).get("")

                        item_ep = ConflictItem(
                            field_name="event_first_priority_date",
                            field_label="Priority date",
                            current_value=current_ep,
                            new_value=earliest_priority_str,
                            table_name="matter_event",
                            field_key="PRIORITY_DATE",
                            priority=2,
                        )
                        _route_plain_item(
                            item=item_ep,
                            new_val=earliest_priority_str,
                            auto_apply=auto_apply,
                            conflicts=conflicts,
                        )

                        if earliest_priority_number:
                            current_priority_no = matter_data.get("custom_fields", {}).get(
                                "priority_no"
                            )
                            item_priority_no = ConflictItem(
                                field_name="custom_priority_no",
                                field_label="Priority",
                                current_value=current_priority_no,
                                new_value=earliest_priority_number,
                                table_name="matter_custom_field",
                                field_key="priority_no",
                                priority=2,
                            )
                            _route_plain_item(
                                item=item_priority_no,
                                new_val=earliest_priority_number,
                                auto_apply=auto_apply,
                                conflicts=conflicts,
                            )

                        current_priority_date = matter_data.get("custom_fields", {}).get(
                            "priority_date"
                        )
                        item_priority_date = ConflictItem(
                            field_name="custom_priority_date",
                            field_label="",
                            current_value=current_priority_date,
                            new_value=earliest_priority_str,
                            table_name="matter_custom_field",
                            field_key="priority_date",
                            priority=2,
                        )
                        _route_plain_item(
                            item=item_priority_date,
                            new_val=earliest_priority_str,
                            auto_apply=auto_apply,
                            conflicts=conflicts,
                        )

                    except Exception as exc:
                        # Best-effort: conflict auto-apply is advisory and should not block extraction.
                        report_swallowed_exception(
                            exc,
                            context="parameter_conflict_detector.detect_conflicts.auto_apply",
                            log_key="parameter_conflict_detector.detect_conflicts.auto_apply",
                            log_window_seconds=300,
                        )

            if "related_applications" in extracted_params:
                existing_parents = matter_data.get("identifiers", {}).get("Parent application No.", [])
                if not isinstance(existing_parents, list):
                    existing_parents = [existing_parents] if existing_parents else []

                added_parents_in_session = set()

                for i, rel_app in enumerate(extracted_params["related_applications"]):
                    number = rel_app.get("number")
                    if not number:
                        continue

                    norm_num = _normalize_identifier(number)

                    is_exist = False
                    for ex in existing_parents:
                        if _normalize_identifier(ex) == norm_num:
                            is_exist = True
                            break

                    if not is_exist and norm_num in added_parents_in_session:
                        is_exist = True

                    if not is_exist:
                        field_name = f"identifier_parent_{i}"
                        item_id = ConflictItem(
                            field_name=field_name,
                            field_label=f"Parent application No. ({number})",
                            current_value=None,
                            new_value=number,
                            table_name="matter_identifier",
                            field_key="Parent application No.",
                            priority=2,
                        )
                        auto_apply.append(item_id)
                        added_parents_in_session.add(norm_num)

            if event_key == "EXAM_REQ":
                raw_text = event.get("raw_text", "")
                if "Billing" in raw_text and "Billing" not in raw_text:
                    exam_requested_val = "Y"
                else:
                    exam_requested_val = "N"
                event_at = event.get("event_at")
                if event_at:
                    exam_request_date_val = event_at
                    force_exam_request_date = True

                _detect_field_value(
                    field_name="exam_requested",
                    current=matter_data.get("custom_fields", {}).get("exam_requested"),
                    new_val=exam_requested_val,
                    field_definitions=field_definitions,
                    auto_apply=auto_apply,
                    conflicts=conflicts,
                )

    if exam_requested_val == "Y" and not exam_request_date_val and app_date_val:
        exam_request_date_val = app_date_val
        force_exam_request_date = False

    if exam_request_date_val:
        current_cf = matter_data.get("custom_fields", {}).get("exam_request_date")
        item_cf = _create_conflict_item(
            field_definitions=field_definitions,
            field_name="exam_request_date",
            current=current_cf,
            new_val=exam_request_date_val,
        )
        if force_exam_request_date and not item_cf.has_conflict:
            _maybe_append_visible(
                bucket=auto_apply,
                item=item_cf,
                field_definitions=field_definitions,
                allow_hidden=True,
            )
        else:
            _route_detected_item(
                item=item_cf,
                new_val=exam_request_date_val,
                auto_apply=auto_apply,
                conflicts=conflicts,
                field_definitions=field_definitions,
            )

    if "party_roles" in extracted_params:
        role_counts: dict[str, int] = {}
        for role in extracted_params["party_roles"]:
            role_code = (role.get("role_code") or "").strip()
            if not role_code:
                continue
            role_key = role_code.upper()
            role_norm = role_code.lower()
            new_val = (role.get("raw_text") or "").strip()
            current_list = (
                matter_data.get("party_roles", {}).get(role_norm)
                or matter_data.get("party_roles", {}).get(role_code)
                or matter_data.get("party_roles", {}).get(role_key)
                or []
            )
            current = ", ".join(current_list) if current_list else None

            label = {
                "APPLICANT": "Applicant",
                "INVENTOR": "Inventor",
                "CREATOR": "",
            }.get(role_key, role_code)

            field_base = f"party_{role_norm}"
            role_counts[field_base] = role_counts.get(field_base, 0) + 1
            field_name = (
                field_base
                if role_counts[field_base] == 1
                else f"{field_base}_{role_counts[field_base]}"
            )

            item = ConflictItem(
                field_name=field_name,
                field_label=label,
                current_value=current,
                new_value=new_val,
                table_name="matter_party_role",
                field_key=role_norm,
                priority=2,
            )

            if role_key in {"INVENTOR", "CREATOR"}:
                if new_val and new_val not in (current_list or []):
                    auto_apply.append(item)
            else:
                if current and new_val not in current_list:
                    conflicts.append(item)
                elif new_val and new_val not in (current_list or []):
                    auto_apply.append(item)

    return ParameterExtractionResult(
        matter_id=matter_id,
        our_ref=our_ref,
        doc_type=extracted_params.get("doc_type", ""),
        auto_apply=auto_apply,
        conflicts=conflicts,
        skipped=skipped,
    )
