from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models.client import Client
from app.models.party import Party, PartyAddress, PartyCode, PartyContact, PartyStaff
from app.utils.error_logging import report_swallowed_exception

_LEGACY_TAX_ADDRESS_TYPE = "Tax " + "document Address"


def _looks_like_email(value: str) -> bool:
    v = (value or "").strip()
    return "@" in v and "." in v and " " not in v


def _pick_best_address(addresses: List[Tuple[str, str]]) -> Optional[str]:
    if not addresses:
        return None

    preferred_types = (
        "Filing Address",
        "Mailing Address",
        "Billing Address",
        "Tax documentation address",
        " Address",
        _LEGACY_TAX_ADDRESS_TYPE,
    )
    by_type: Dict[str, List[str]] = {}
    for address_type, address_text in addresses:
        by_type.setdefault(address_type or "", []).append(address_text or "")

    for t in preferred_types:
        for addr in by_type.get(t, []):
            if addr and not _looks_like_email(addr):
                return addr.strip()

    for _, addr in addresses:
        if addr and not _looks_like_email(addr):
            return addr.strip()

    for _, addr in addresses:
        if addr:
            return addr.strip()

    return None


def _pick_address_of_type(
    addresses: List[Tuple[str, str]], address_type: str, *, allow_email_like: bool = False
) -> Optional[str]:
    target = (address_type or "").replace(" ", "")
    for t, addr in addresses:
        current = (t or "").replace(" ", "")
        if current != target:
            continue
        v = (addr or "").strip()
        if not v:
            continue
        if not allow_email_like and _looks_like_email(v):
            continue
        return v
    return None


def _pick_first_contact(contacts: List[Tuple[str, str, str]], contact_type: str) -> Optional[str]:
    target = (contact_type or "").strip().lower()
    candidates = {target}
    if target == "email":
        candidates.update(["e-mail", "Email", "", "email address", ""])
    elif target == "phone":
        candidates.update(
            [
                "mobile",
                "cell",
                "tel",
                "telephone",
                "",
                "",
                "Phone",
                "handphone",
                "mobile phone",
                "hp",
            ]
        )
    elif target == "fax":
        candidates.update(["", "facsimile"])

    for ctype, _label, value in contacts:
        ct = (ctype or "").strip().lower()
        if ct in candidates:
            v = (value or "").strip()
            if v:
                return v
    return None


def ensure_clients_synced_from_party(*, include_staff: bool = False) -> int:
    """
    Sync ipm `party` rows into app `clients` table so legacy CRM/search can work.

    Returns number of inserted/updated Client rows during this call.
    """
    from datetime import datetime

    from app.models.ip_records import MatterPartyRole

    # NOTE: Column addition is handled by explicit schema initialization.

    party_count_total = Party.query.count()
    if party_count_total <= 0:
        return 0

    chunk_size = 1000
    try:
        chunk_size = int(current_app.config.get("CLIENT_PARTY_SYNC_CHUNK_SIZE", chunk_size))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="client_party_sync.chunk_size",
            log_key="client_party_sync.chunk_size",
            log_window_seconds=300,
        )
    if chunk_size <= 0:
        chunk_size = 1000

    def _chunked(values: List[str], size: int):
        for i in range(0, len(values), size):
            yield values[i : i + size]

    staff_party_ids: set[str] = set()
    if not include_staff:
        staff_party_ids = {pid for (pid,) in db.session.query(PartyStaff.party_id).all() if pid}

    # Exclude inventors/creators: parties that ONLY have inventor role (no client/applicant role)
    inventor_party_ids: set[str] = {
        pid
        for (pid,) in db.session.query(MatterPartyRole.party_id)
        .filter(func.lower(MatterPartyRole.role_code).in_(["inventor", "creator"]))
        .filter(MatterPartyRole.party_id.isnot(None))
        .distinct()
        .all()
        if pid
    }
    client_role_party_ids: set[str] = {
        pid
        for (pid,) in db.session.query(MatterPartyRole.party_id)
        .filter(MatterPartyRole.role_code.in_(["applicant", "client"]))
        .filter(MatterPartyRole.party_id.isnot(None))
        .distinct()
        .all()
        if pid
    }
    inventor_only_party_ids = inventor_party_ids - client_role_party_ids

    # Soft-delete existing clients that are now identified as inventors only
    deleted_count = 0
    if inventor_only_party_ids:
        now = datetime.utcnow()
        for chunk in _chunked(list(inventor_only_party_ids), chunk_size):
            updated = Client.query.filter(
                Client.party_id.in_(chunk),
                (Client.is_deleted.is_(False)) | (Client.is_deleted.is_(None)),
            ).update({"is_deleted": True, "deleted_at": now}, synchronize_session=False)
            if updated:
                deleted_count += int(updated)
                db.session.commit()
        if deleted_count:
            try:
                current_app.logger.info("Soft-deleted %d inventor-only clients", deleted_count)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_party_sync.log_deleted",
                    log_key="client_party_sync.log_deleted",
                    log_window_seconds=300,
                )

    target_count = (
        party_count_total if include_staff else max(0, party_count_total - len(staff_party_ids))
    )
    mapped_count = Client.query.filter(Client.party_id.isnot(None)).count()

    inserted_or_updated = 0

    def _process_party_chunk(rows: List[Tuple[str, str, str, str, str, str, str, str]]) -> int:
        if not rows:
            return 0
        party_ids = [
            pid
            for (pid, *_rest) in rows
            if pid
            and (include_staff or pid not in staff_party_ids)
            and pid not in inventor_only_party_ids
        ]
        if not party_ids:
            return 0

        existing_clients = Client.query.filter(Client.party_id.in_(party_ids)).all()
        existing_by_party_id: Dict[str, Client] = {
            c.party_id: c for c in existing_clients if c.party_id
        }
        deleted_party_ids: set[str] = {
            c.party_id for c in existing_clients if c.party_id and c.is_deleted
        }

        contacts_rows = (
            db.session.query(
                PartyContact.party_id,
                PartyContact.contact_type,
                PartyContact.label,
                PartyContact.value,
            )
            .filter(PartyContact.party_id.in_(party_ids))
            .all()
        )
        contacts_by_party: Dict[str, List[Tuple[str, str, str]]] = {}
        for pid, ctype, label, value in contacts_rows:
            if not pid:
                continue
            contacts_by_party.setdefault(pid, []).append((ctype or "", label or "", value or ""))

        addr_rows = (
            db.session.query(
                PartyAddress.party_id,
                PartyAddress.address_type,
                PartyAddress.address_text,
            )
            .filter(PartyAddress.party_id.in_(party_ids))
            .all()
        )
        addrs_by_party: Dict[str, List[Tuple[str, str]]] = {}
        for pid, atype, atext in addr_rows:
            if not pid:
                continue
            addrs_by_party.setdefault(pid, []).append((atype or "", atext or ""))

        code_rows = (
            db.session.query(
                PartyCode.party_id,
                PartyCode.code_type,
                PartyCode.code_value,
            )
            .filter(PartyCode.party_id.in_(party_ids))
            .all()
        )
        codes_by_party: Dict[str, List[Tuple[str, str]]] = {}
        for pid, ctype, cval in code_rows:
            if not pid:
                continue
            codes_by_party.setdefault(pid, []).append((ctype or "", cval or ""))

        chunk_updates = 0
        for (
            pid,
            name_display,
            name_en,
            nationality,
            reg_no,
            business_no,
            created_at,
        ) in rows:
            if not pid or (not include_staff and pid in staff_party_ids):
                continue
            if pid in inventor_only_party_ids:
                continue
            if pid in deleted_party_ids:
                continue

            name = (name_display or name_en or "").strip() or "(unknown)"
            contacts = sorted(
                contacts_by_party.get(pid, []),
                key=lambda x: ((x[0] or "").lower(), (x[1] or ""), (x[2] or "")),
            )
            email = _pick_first_contact(contacts, "email")
            phone = _pick_first_contact(contacts, "phone")
            fax = _pick_first_contact(contacts, "fax")
            addresses = sorted(
                addrs_by_party.get(pid, []),
                key=lambda x: ((x[0] or ""), (x[1] or "")),
            )
            address = _pick_best_address(addresses)
            mail_recv_address = _pick_address_of_type(addresses, "Mailing Address")
            if not mail_recv_address:
                mail_recv_address = _pick_address_of_type(addresses, " Address")
            tax_address = _pick_address_of_type(addresses, "Tax documentation address")
            if not tax_address:
                tax_address = _pick_address_of_type(addresses, _LEGACY_TAX_ADDRESS_TYPE)
            registration_number = (business_no or reg_no or "").strip() or None

            pcodes = codes_by_party.get(pid, [])
            applicant_codes = sorted(
                [val for (ctype, val) in pcodes if ctype == "applicant_code" and val]
            )
            client_code_list = [val for (ctype, val) in pcodes if ctype == "client_code" and val]
            client_code = client_code_list[0] if client_code_list else None

            contacts_payload = sorted(
                [
                    {"type": ctype, "label": label, "value": value}
                    for (ctype, label, value) in contacts
                    if (value or "").strip()
                ],
                key=lambda x: (
                    (x.get("type") or "").lower(),
                    x.get("label") or "",
                    x.get("value") or "",
                ),
            )
            addresses_payload = sorted(
                [
                    {"type": atype, "value": value}
                    for (atype, value) in addresses
                    if (value or "").strip()
                ],
                key=lambda x: (x.get("type") or "", x.get("value") or ""),
            )

            extra = {
                "source": "ipm_party",
                "party_id": pid,
                "name_en": (name_en or "").strip() or None,
                "nationality": (nationality or "").strip() or None,
                "reg_no": (reg_no or "").strip() or None,
                "business_no": (business_no or "").strip() or None,
                "party_created_at": created_at,
                # CRM form-friendly keys (best-effort)
                "business_reg_no": (business_no or "").strip() or None,
                "applicant_email": email,
                "applicant_phone": phone,
                "main_phone": phone,
                "mobile_phone": phone,
                "main_fax": fax,
                "applicant_fax": fax,
                "personal_fax": fax,
                "other_fax": fax,
                "mail_recv_address": mail_recv_address,
                "tax_address": tax_address,
                "applicant_codes": applicant_codes,
                "client_code": client_code,
                "contacts": contacts_payload,
                "addresses": addresses_payload,
            }

            existing = existing_by_party_id.get(pid)
            if existing is None:
                client = Client(
                    party_id=pid,
                    name=name,
                    email=email,
                    phone=phone,
                    address=address,
                    registration_number=registration_number,
                    type=None,
                    extra=extra,
                )
                db.session.add(client)
                existing_by_party_id[pid] = client
                chunk_updates += 1
                continue

            existing_extra = existing.extra or {}
            if (existing_extra.get("source") or "") != "ipm_party":
                continue

            changed = False
            if existing.party_id != pid:
                existing.party_id = pid
                changed = True
            for attr, new_value in (
                ("name", name),
                ("email", email),
                ("phone", phone),
                ("address", address),
                ("registration_number", registration_number),
            ):
                if getattr(existing, attr) != new_value:
                    setattr(existing, attr, new_value)
                    changed = True

            if existing_extra != extra:
                existing.extra = extra
                changed = True
            if changed:
                chunk_updates += 1

        if chunk_updates:
            db.session.commit()
        return chunk_updates

    party_query = (
        Party.query.with_entities(
            Party.party_id,
            Party.name_display,
            Party.name_en,
            Party.nationality,
            Party.reg_no,
            Party.business_no,
            Party.created_at,
        )
        .order_by(Party.party_id)
        .yield_per(chunk_size)
    )
    chunk_rows: List[Tuple[str, str, str, str, str, str, str]] = []
    for row in party_query:
        chunk_rows.append(row)
        if len(chunk_rows) >= chunk_size:
            inserted_or_updated += _process_party_chunk(chunk_rows)
            chunk_rows = []
    if chunk_rows:
        inserted_or_updated += _process_party_chunk(chunk_rows)

    if inserted_or_updated:
        try:
            current_app.logger.info(
                "Synced clients from party: changed=%s mapped=%s/%s",
                inserted_or_updated,
                mapped_count,
                target_count,
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_party_sync.log_summary",
                log_key="client_party_sync.log_summary",
                log_window_seconds=300,
            )

    return inserted_or_updated
