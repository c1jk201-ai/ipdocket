from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import load_only

from app.models.ip_records import DocketItem, Matter
from app.models.workflow import Workflow
from app.utils.docket_visibility import is_visible_by_date
from app.utils.workflow_semantics import workflow_badge_values

_DOCKET_BC_RE = re.compile(r"^DOCKET:([^:]+)", re.IGNORECASE)
_AUTO_DOCKET_NOTE_MARKER = "Auto Create: DocketItem "


def _docket_id_from_business_code(raw_business_code: str | None) -> str | None:
    bc = (raw_business_code or "").strip()
    m = _DOCKET_BC_RE.match(bc)
    if not m:
        return None
    docket_id = (m.group(1) or "").strip()
    return docket_id or None


def _workflow_docket_id(wf: Workflow) -> str | None:
    if not wf:
        return None
    return _docket_id_from_business_code(getattr(wf, "business_code", None))


def _hidden_docket_workflow_ids(
    *,
    workflow_refs: list[tuple[int, str | None]],
    today: date,
) -> set[int]:
    docket_id_by_wf_id: dict[int, str] = {}
    docket_ids: set[str] = set()

    for wf_id, business_code in workflow_refs or []:
        did = _docket_id_from_business_code(business_code)
        if not did:
            continue
        docket_id_by_wf_id[int(wf_id)] = did
        docket_ids.add(did)

    if not docket_ids:
        return set()

    docket_rows = (
        DocketItem.query.options(
            load_only(
                DocketItem.docket_id,
                DocketItem.is_deleted,
                DocketItem.visible_from_date,
            )
        )
        .filter(DocketItem.docket_id.in_(sorted(docket_ids)))
        .all()
    )
    docket_by_id: dict[str, DocketItem] = {}
    for di in docket_rows:
        did = str(getattr(di, "docket_id", "") or "").strip()
        if did:
            docket_by_id[did] = di

    hidden_docket_ids: set[str] = set()
    for did in docket_ids:
        di = docket_by_id.get(did)
        if di is None:
            continue
        if bool(getattr(di, "is_deleted", False)):
            hidden_docket_ids.add(did)
            continue
        if not is_visible_by_date(di, today=today):
            hidden_docket_ids.add(did)

    return {wf_id for wf_id, did in docket_id_by_wf_id.items() if did in hidden_docket_ids}


def _merge_task_statuses(statuses: set[str]) -> str:
    normalized = {(s or "").strip().lower() for s in statuses if (s or "").strip()}
    if "overdue" in normalized:
        return "overdue"
    if "urgent" in normalized:
        return "urgent"
    if "pending" in normalized:
        return "pending"
    if "completed" in normalized:
        return "completed"
    if "abandoned" in normalized:
        return "abandoned"
    return "pending"


def _merge_csv_names(values: list[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        parts = [p.strip() for p in str(raw or "").split(",")]
        for part in parts:
            if not part or part in seen:
                continue
            seen.add(part)
            out.append(part)
    return ", ".join(out)


def _staff_row_names(rows: list[dict]) -> str:
    return _merge_csv_names(
        [
            str(row.get("name") or "").strip()
            for row in (rows or [])
            if str(row.get("name") or "").strip()
        ]
    )


def _task_has_completion_recommendation(task: dict) -> bool:
    return bool(task.get("completion_recommendation"))


def _dedupe_staff_rows(values: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen_keys: set[str] = set()
    for row in values or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        rname = str(row.get("name") or "").strip()
        if not rid and not rname:
            continue
        key = rid or rname
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append({"id": rid or None, "name": rname})
    return rows


def _merge_staff_rows(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for group in groups:
        for row in group or []:
            if not isinstance(row, dict):
                continue
            merged.append(
                {
                    "id": (str(row.get("id") or "").strip() or None),
                    "name": str(row.get("name") or "").strip(),
                }
            )
    return _dedupe_staff_rows(merged)


def _category_types_from_value(raw: str | None) -> list[str]:
    s = str(raw or "").strip().lower()
    if not s:
        return []
    if s in ("mixed", "hybrid"):
        return ["mgmt", "work"]
    if s in ("mgmt", "work"):
        return [s]
    if s in ("mgmt_work", "work_mgmt"):
        return ["mgmt", "work"]
    return []


def _category_labels_from_value(raw: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").split(","):
        label = part.strip()
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out


def _workflow_category_badge_values(raw_category: str | None) -> tuple[str, str]:
    return workflow_badge_values(raw_category)


def _workflow_is_auto_docket_generated(wf: Workflow) -> bool:
    docket_id = _workflow_docket_id(wf)
    return bool(docket_id and _AUTO_DOCKET_NOTE_MARKER in str(getattr(wf, "note", "") or ""))


def _worklog_group_key_for_workflow(wf: Workflow) -> str:
    docket_id = _workflow_docket_id(wf)
    if docket_id and _workflow_is_auto_docket_generated(wf):
        return f"docket:{docket_id}"
    return f"wf:{int(getattr(wf, 'id', 0) or 0)}"


def _filter_hidden_workflow_rows(
    rows: list[tuple[Workflow, Matter]],
    *,
    today: date,
) -> list[tuple[Workflow, Matter]]:
    hidden_wf_ids = _hidden_docket_workflow_ids(
        workflow_refs=[
            (int(getattr(wf, "id", 0) or 0), getattr(wf, "business_code", None))
            for wf, _m in rows
            if int(getattr(wf, "id", 0) or 0) > 0
        ],
        today=today,
    )
    if not hidden_wf_ids:
        return rows
    return [row for row in rows if int(getattr(row[0], "id", 0) or 0) not in hidden_wf_ids]


def _filter_hidden_workflows(
    workflows: list[Workflow],
    *,
    today: date,
) -> list[Workflow]:
    hidden_wf_ids = _hidden_docket_workflow_ids(
        workflow_refs=[
            (int(getattr(wf, "id", 0) or 0), getattr(wf, "business_code", None))
            for wf in workflows
            if int(getattr(wf, "id", 0) or 0) > 0
        ],
        today=today,
    )
    if not hidden_wf_ids:
        return workflows
    return [wf for wf in workflows if int(getattr(wf, "id", 0) or 0) not in hidden_wf_ids]


def _non_empty_iso_values(values: list[object] | None) -> list[str]:
    return [str(v).strip() for v in values or [] if str(v or "").strip()]


def _dedupe_text_values(values: list[object] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _merge_task_row_into_groups(
    task: dict,
    *,
    grouped: dict[str, dict],
    ordered_keys: list[str],
) -> None:
    docket_id = str(task.get("_linked_docket_id") or "").strip()
    is_auto_docket = bool(task.get("_auto_docket_generated"))
    key = f"docket:{docket_id}" if docket_id and is_auto_docket else f"wf:{task.get('id')}"

    bucket = grouped.get(key)
    if bucket is None:
        row = dict(task)
        if docket_id and is_auto_docket:
            row["id"] = docket_id
        row["_statuses"] = {(row.get("status") or "pending").strip().lower()}
        row["_category_labels"] = []
        row["_category_types"] = []
        row["_owner_rows"] = list(row.get("owners") or [])
        row["_task_names"] = []
        row["_due_values"] = []
        row["_final_due_values"] = []
        row["_internal_due_values"] = []
        row["_done_values"] = []
        row["_workflow_link_ids"] = []
        row["_completion_reco_texts"] = []
        row["_attorneys_rows"] = list(row.get("attorneys") or [])
        row["_handlers_rows"] = list(row.get("handlers") or [])
        row["_managers_rows"] = list(row.get("managers") or [])
        row["_workflow_assignee_filter_ids"] = list(row.get("_workflow_assignee_filter_ids") or [])

        for label in _category_labels_from_value(row.get("category_display")):
            if label not in row["_category_labels"]:
                row["_category_labels"].append(label)
        for ctype in _category_types_from_value(row.get("category_type")):
            if ctype not in row["_category_types"]:
                row["_category_types"].append(ctype)

        if not row["_owner_rows"]:
            owner_name = str(row.get("owner_name") or "").strip()
            owner_id = str(row.get("owner_id") or "").strip()
            if owner_name or owner_id:
                row["_owner_rows"].append({"id": owner_id or None, "name": owner_name or owner_id})

        for key_name, bucket_name in (
            ("task_name", "_task_names"),
            ("due_date", "_due_values"),
            ("final_due_date", "_final_due_values"),
            ("internal_due_date", "_internal_due_values"),
            ("done_date", "_done_values"),
            ("workflow_link_id", "_workflow_link_ids"),
        ):
            value = str(row.get(key_name) or "").strip()
            if value:
                row[bucket_name].append(value)

        if bool(row.get("completion_recommendation")):
            txt = str(row.get("completion_recommendation_text") or "").strip()
            if txt:
                row["_completion_reco_texts"].append(txt)

        grouped[key] = row
        ordered_keys.append(key)
        return

    bucket["_statuses"].add((task.get("status") or "pending").strip().lower())

    for label in _category_labels_from_value(task.get("category_display")):
        if label not in bucket["_category_labels"]:
            bucket["_category_labels"].append(label)
    for ctype in _category_types_from_value(task.get("category_type")):
        if ctype not in bucket["_category_types"]:
            bucket["_category_types"].append(ctype)

    task_owner_rows = list(task.get("owners") or [])
    if task_owner_rows:
        bucket["_owner_rows"].extend(task_owner_rows)
    else:
        owner_name = str(task.get("owner_name") or "").strip()
        owner_id = str(task.get("owner_id") or "").strip()
        if owner_name or owner_id:
            owner_key = owner_id or owner_name
            known = {(o.get("id") or o.get("name") or "") for o in bucket["_owner_rows"]}
            if owner_key not in known:
                bucket["_owner_rows"].append(
                    {"id": owner_id or None, "name": owner_name or owner_id}
                )

    task_name = str(task.get("task_name") or "").strip()
    if task_name and task_name not in bucket["_task_names"]:
        bucket["_task_names"].append(task_name)

    for key_name, bucket_name in (
        ("due_date", "_due_values"),
        ("final_due_date", "_final_due_values"),
        ("internal_due_date", "_internal_due_values"),
        ("done_date", "_done_values"),
    ):
        value = str(task.get(key_name) or "").strip()
        if value:
            bucket[bucket_name].append(value)

    workflow_link_id = str(task.get("workflow_link_id") or "").strip()
    if workflow_link_id and workflow_link_id not in bucket["_workflow_link_ids"]:
        bucket["_workflow_link_ids"].append(workflow_link_id)

    if bool(task.get("completion_recommendation")):
        bucket["completion_recommendation"] = True
        if not str(bucket.get("completion_recommendation_kind") or "").strip():
            bucket["completion_recommendation_kind"] = task.get("completion_recommendation_kind")
        txt = str(task.get("completion_recommendation_text") or "").strip()
        if txt and txt not in bucket["_completion_reco_texts"]:
            bucket["_completion_reco_texts"].append(txt)

    bucket["_attorneys_rows"].extend(list(task.get("attorneys") or []))
    bucket["_handlers_rows"].extend(list(task.get("handlers") or []))
    bucket["_managers_rows"].extend(list(task.get("managers") or []))
    bucket["_workflow_assignee_filter_ids"].extend(
        list(task.get("_workflow_assignee_filter_ids") or [])
    )


def _finalize_merged_task_bucket(row: dict) -> dict:
    category_order = {"mgmt": 0, "work": 1}
    category_label_by_type = {"mgmt": "", "work": ""}

    row["status"] = _merge_task_statuses(set(row.pop("_statuses", set())))

    due_values = _non_empty_iso_values(row.pop("_due_values", []))
    row["due_date"] = min(due_values) if due_values else None
    row["original_due"] = row.get("due_date")
    final_due_values = _non_empty_iso_values(row.pop("_final_due_values", []))
    row["final_due_date"] = min(final_due_values) if final_due_values else None
    internal_due_values = _non_empty_iso_values(row.pop("_internal_due_values", []))
    row["internal_due_date"] = min(internal_due_values) if internal_due_values else None
    row["extended_due"] = row.get("internal_due_date")

    done_values = _non_empty_iso_values(row.pop("_done_values", []))
    row["done_date"] = max(done_values) if done_values else None

    task_names = [n for n in row.pop("_task_names", []) if n]
    if task_names:
        row["task_name"] = task_names[0] if len(task_names) == 1 else " / ".join(task_names)

    workflow_link_ids = [v for v in row.pop("_workflow_link_ids", []) if v]
    if row.get("workflow_link_id") is None:
        row["workflow_link_id"] = workflow_link_ids[0] if workflow_link_ids else None
    if not row.get("workflow_link_id"):
        fallback = str(row.get("id") or "").strip()
        if fallback.startswith("wf_"):
            row["workflow_link_id"] = fallback

    category_types = [t for t in row.pop("_category_types", []) if t in ("mgmt", "work")]
    labels = [c for c in row.pop("_category_labels", []) if c]
    label_to_type = {"": "mgmt", "": "work"}
    for label in labels:
        if label == "HYBRID":
            for mapped in ("mgmt", "work"):
                if mapped not in category_types:
                    category_types.append(mapped)
            continue
        mapped = label_to_type.get(label)
        if mapped and mapped not in category_types:
            category_types.append(mapped)
    if category_types:
        uniq_types = sorted(set(category_types), key=lambda v: category_order.get(v, 99))
        if len(uniq_types) == 2:
            row["category_type"] = "hybrid"
            row["category_display"] = "HYBRID"
        else:
            one = uniq_types[0]
            row["category_type"] = one
            row["category_display"] = category_label_by_type.get(one, "")
    elif labels:
        deduped = []
        for label in labels:
            if label not in deduped:
                deduped.append(label)
        if len(deduped) > 1 or deduped == ["HYBRID"]:
            row["category_display"] = "HYBRID"
            row["category_type"] = "hybrid"
        else:
            row["category_display"] = deduped[0]
            row["category_type"] = "work"
    else:
        row["category_type"] = "work"
        row["category_display"] = ""

    owners = _dedupe_staff_rows(row.pop("_owner_rows", []))
    row["owners"] = owners
    owner_names = [
        str(o.get("name") or "").strip() for o in owners if str(o.get("name") or "").strip()
    ]
    row["owner_name"] = _merge_csv_names(owner_names)
    if len(owners) == 1 and str(owners[0].get("id") or "").strip():
        row["owner_id"] = str(owners[0]["id"]).strip()
        if not row["owner_name"]:
            row["owner_name"] = row["owner_id"]
    else:
        row["owner_id"] = None

    row["attorneys"] = _dedupe_staff_rows(row.pop("_attorneys_rows", []))
    row["handlers"] = _dedupe_staff_rows(row.pop("_handlers_rows", []))
    row["managers"] = _dedupe_staff_rows(row.pop("_managers_rows", []))
    row["attorney_names"] = _merge_csv_names(
        [str(p.get("name") or "") for p in row.get("attorneys") or []]
    )
    row["handler_names"] = _merge_csv_names(
        [str(p.get("name") or "") for p in row.get("handlers") or []]
    )
    row["manager_names"] = _merge_csv_names(
        [str(p.get("name") or "") for p in row.get("managers") or []]
    )
    row["_workflow_assignee_filter_ids"] = _dedupe_text_values(
        row.pop("_workflow_assignee_filter_ids", [])
    )

    reco_texts = [t for t in row.pop("_completion_reco_texts", []) if t]
    if reco_texts:
        row["completion_recommendation_text"] = reco_texts[0]

    row.pop("_linked_docket_id", None)
    row.pop("_auto_docket_generated", None)
    row.pop("_intake_confirmation_task", None)
    return row


def _merge_docket_autogen_tasks(tasks: list[dict]) -> list[dict]:
    ordered_keys: list[str] = []
    grouped: dict[str, dict] = {}

    for task in tasks:
        _merge_task_row_into_groups(task, grouped=grouped, ordered_keys=ordered_keys)

    return [_finalize_merged_task_bucket(grouped[key]) for key in ordered_keys]
