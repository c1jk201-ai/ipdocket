from __future__ import annotations

POST_REGISTRATION_MGMT_DOCKET_REFS = frozenset(
    {
        "MGMT:REG_CERT:RECEIPT",
        "MGMT:REG_CERT:SEND_3D",
    }
)


def is_post_registration_mgmt_docket_ref(name_ref: object) -> bool:
    return str(name_ref or "").strip().upper() in POST_REGISTRATION_MGMT_DOCKET_REFS


def is_post_registration_mgmt_docket(docket_item: object | None) -> bool:
    if docket_item is None:
        return False
    return is_post_registration_mgmt_docket_ref(getattr(docket_item, "name_ref", None))


def docket_id_from_workflow_business_code(business_code: object) -> str:
    raw = str(business_code or "").strip()
    if not raw.upper().startswith("DOCKET:"):
        return ""
    parts = raw.split(":", 2)
    if len(parts) < 2:
        return ""
    return str(parts[1] or "").strip()
