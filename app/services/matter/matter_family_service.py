from __future__ import annotations

import re
import uuid
from datetime import datetime

from app.extensions import db
from app.models.ip_records import Family, Matter, MatterFamily
from app.utils.permissions import can_access_matter

_BASE_REF_RE = re.compile(r"^(\d{2}[A-Z]{2}\d{4})", re.IGNORECASE)


def _extract_base_ref(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    m = _BASE_REF_RE.match(raw)
    if not m:
        return ""
    return m.group(1).upper()


def _generate_manual_key() -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:6].upper()
    return f"MANUAL-{stamp}-{suffix}"


def _ensure_unique_family_key(base_key: str) -> str:
    key = (base_key or "").strip().upper()
    if not key:
        key = _generate_manual_key()
    if not Family.query.filter_by(family_key=key).first():
        return key
    for i in range(1, 1000):
        candidate = f"{key}-{i}"
        if not Family.query.filter_by(family_key=candidate).first():
            return candidate
    return _generate_manual_key()


def _resolve_family_for_link(
    *,
    primary_ids: list[str],
    target_ids: list[str],
    explicit_family_key: str | None = None,
    prefer_primary: bool = True,
    primary_ref: str = "",
    target_ref: str = "",
) -> tuple[Family, bool]:
    # 1) explicit key (user chosen)
    if explicit_family_key:
        key = explicit_family_key.strip()
        if key:
            fam = Family.query.filter_by(family_key=key).first()
            if fam:
                return fam, False
            fam = Family(
                family_id=uuid.uuid4().hex,
                family_key=_ensure_unique_family_key(key),
                key_type="manual",
                key_value=key,
                created_at=datetime.utcnow().isoformat(),
            )
            db.session.add(fam)
            return fam, True

    # 2) already shared
    common = {f for f in primary_ids if f}.intersection({f for f in target_ids if f})
    if common:
        fam_id = next(iter(common))
        fam = Family.query.filter_by(family_id=fam_id).first()
        if fam:
            return fam, False

    # 3) reuse existing family from either side
    preferred = primary_ids if prefer_primary else target_ids
    fallback = target_ids if prefer_primary else primary_ids
    for fam_id in preferred + fallback:
        if not fam_id:
            continue
        fam = Family.query.filter_by(family_id=fam_id).first()
        if fam:
            return fam, False

    # 4) create new (or reuse by key)
    base_key = _extract_base_ref(primary_ref) or _extract_base_ref(target_ref)
    if not base_key:
        base_key = (primary_ref or target_ref or "").strip().upper()
    if base_key:
        fam = Family.query.filter_by(family_key=base_key).first()
        if fam:
            return fam, False
    final_key = _ensure_unique_family_key(base_key)
    fam = Family(
        family_id=uuid.uuid4().hex,
        family_key=final_key,
        key_type="manual",
        key_value=final_key,
        created_at=datetime.utcnow().isoformat(),
    )
    db.session.add(fam)
    return fam, True


def _merge_families_into(*, canonical_family_id: str, family_ids_to_merge: list[str]) -> int:
    canonical = (canonical_family_id or "").strip()
    merge_ids = {(fid or "").strip() for fid in (family_ids_to_merge or []) if (fid or "").strip()}
    merge_ids.discard(canonical)
    if not canonical or not merge_ids:
        return 0

    merged_rows = (
        MatterFamily.query.filter(MatterFamily.family_id.in_(sorted(merge_ids)))
        .order_by(MatterFamily.created_at.asc(), MatterFamily.mf_id.asc())
        .all()
    )
    canonical_mids = {
        (mid or "").strip()
        for (mid,) in (
            db.session.query(MatterFamily.matter_id)
            .filter(MatterFamily.family_id == canonical)
            .distinct()
            .all()
        )
        if (mid or "").strip()
    }
    changed = 0
    for row in merged_rows:
        mid = (row.matter_id or "").strip()
        if not mid:
            db.session.delete(row)
            changed += 1
            continue
        if mid in canonical_mids:
            db.session.delete(row)
            changed += 1
            continue
        row.family_id = canonical
        canonical_mids.add(mid)
        changed += 1

    # Defensive dedupe for pre-existing duplicate rows under canonical family.
    seen_mids: set[str] = set()
    canonical_rows = (
        MatterFamily.query.filter(MatterFamily.family_id == canonical)
        .order_by(MatterFamily.created_at.asc(), MatterFamily.mf_id.asc())
        .all()
    )
    for row in canonical_rows:
        mid = (row.matter_id or "").strip()
        if not mid or mid in seen_mids:
            db.session.delete(row)
            changed += 1
            continue
        seen_mids.add(mid)

    Family.query.filter(Family.family_id.in_(sorted(merge_ids))).delete(synchronize_session=False)
    return changed


def link_matters_into_family(
    *,
    primary_matter: Matter,
    target_matter: Matter,
    explicit_family_key: str | None = None,
    prefer_primary: bool = True,
    link_role: str = "manual",
    actor=None,
) -> tuple[str, str, bool]:
    """
    Ensure two matters are linked to a single family.
    Returns (family_id, family_key, created_new_family).
    """
    if not primary_matter or not target_matter:
        raise ValueError("Both matters are required.")
    if str(primary_matter.matter_id) == str(target_matter.matter_id):
        raise ValueError("Cannot link a matter to itself.")
    if actor is not None and not can_access_matter(
        actor, str(target_matter.matter_id), action="edit_case"
    ):
        raise PermissionError("No permission to link target matter.")

    primary_ids = [
        (r.family_id or "").strip()
        for r in MatterFamily.query.filter_by(matter_id=str(primary_matter.matter_id)).all()
        if (r.family_id or "").strip()
    ]
    target_ids = [
        (r.family_id or "").strip()
        for r in MatterFamily.query.filter_by(matter_id=str(target_matter.matter_id)).all()
        if (r.family_id or "").strip()
    ]

    family, created = _resolve_family_for_link(
        primary_ids=primary_ids,
        target_ids=target_ids,
        explicit_family_key=explicit_family_key,
        prefer_primary=prefer_primary,
        primary_ref=primary_matter.our_ref or "",
        target_ref=target_matter.our_ref or "",
    )
    _merge_families_into(
        canonical_family_id=(family.family_id or "").strip(),
        family_ids_to_merge=primary_ids + target_ids,
    )

    now = datetime.utcnow().isoformat()
    for mid in {str(primary_matter.matter_id), str(target_matter.matter_id)}:
        if not MatterFamily.query.filter_by(matter_id=mid, family_id=family.family_id).first():
            db.session.add(
                MatterFamily(
                    mf_id=uuid.uuid4().hex,
                    matter_id=mid,
                    family_id=family.family_id,
                    link_role=link_role,
                    created_at=now,
                )
            )

    return family.family_id, family.family_key, created


def link_matter_to_family_id(
    *,
    matter: Matter,
    family_id: str,
    link_role: str = "manual",
    actor=None,
) -> tuple[str, str]:
    if not matter:
        raise ValueError("Matter is required.")
    family_id = (family_id or "").strip()
    if not family_id:
        raise ValueError("family_id is required.")
    family = Family.query.filter_by(family_id=family_id).first()
    if not family:
        raise ValueError("Family not found.")
    if actor is not None:
        family_member_ids = [
            (mid or "").strip()
            for (mid,) in (
                db.session.query(MatterFamily.matter_id)
                .filter(MatterFamily.family_id == family.family_id)
                .distinct()
                .all()
            )
            if (mid or "").strip()
        ]
        for mid in family_member_ids:
            if not can_access_matter(actor, mid, action="edit_case"):
                raise PermissionError("No permission to link into this family.")
    if not MatterFamily.query.filter_by(
        matter_id=str(matter.matter_id), family_id=family.family_id
    ).first():
        db.session.add(
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(matter.matter_id),
                family_id=family.family_id,
                link_role=link_role,
                created_at=datetime.utcnow().isoformat(),
            )
        )
    return family.family_id, family.family_key
