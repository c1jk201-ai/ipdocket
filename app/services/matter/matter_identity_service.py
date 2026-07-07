"""Matter identity and legacy reference matching.

Matter is the canonical case-like model. This service owns the compatibility
rules that still need to translate legacy references such as Case.ref_no into
Matter.matter_id.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func, or_

from app.extensions import db
from app.models.ip_records import Matter

logger = logging.getLogger(__name__)


def normalize_reference(value: str | None) -> str:
    """Normalize a reference for tolerant equality checks."""
    return re.sub(r"[^0-9A-Za-z-]", "", (value or "").strip().upper())


_YEAR_FIRST_REF_RE = re.compile(r"^(\d{2})([A-Z]{1,3})(\d{3,4})(PCT|[A-Z]{0,2})$")
_CODE_FIRST_REF_RE = re.compile(r"^([A-Z]{1,2})(\d{2})(\d{3,4})(PCT|[A-Z]{0,2})$")


def _toggle_sequence_zero_padding(seq: str) -> str | None:
    if len(seq) == 3:
        return seq.zfill(4)
    if len(seq) == 4 and seq.startswith("0"):
        return seq[1:]
    return None


def reference_padding_variants(reference: str | None) -> list[str]:
    """Return conservative 3/4 digit sequence variants for our-ref-like values."""
    norm = normalize_reference(reference)
    if not norm:
        return []

    variants: list[str] = []
    m = _YEAR_FIRST_REF_RE.match(norm)
    if m:
        year, code, seq, suffix = m.groups()
        alt_seq = _toggle_sequence_zero_padding(seq)
        if alt_seq:
            variants.append(f"{year}{code}{alt_seq}{suffix}")
    else:
        m = _CODE_FIRST_REF_RE.match(norm)
        if m:
            code, year, seq, suffix = m.groups()
            alt_seq = _toggle_sequence_zero_padding(seq)
            if alt_seq:
                variants.append(f"{code}{year}{alt_seq}{suffix}")

    return [v for v in variants if v and v != norm]


@dataclass(frozen=True)
class MatterReferenceMatch:
    matter_id: str
    matched_field: str
    matched_value: str
    normalized: bool = False


class MatterIdentityService:
    """Centralized Matter identity resolution and reference matching."""

    REFERENCE_FIELDS = ("our_ref", "old_our_ref", "your_ref")

    @staticmethod
    def active_query():
        q = Matter.query
        if hasattr(Matter, "is_deleted"):
            q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
        return q

    @classmethod
    def reference_filter(cls, reference: str):
        ref = (reference or "").strip()
        return or_(
            Matter.our_ref == ref,
            Matter.old_our_ref == ref,
            Matter.your_ref == ref,
        )

    @classmethod
    def _normalized_reference_expr(cls, column):
        try:
            dialect = (db.session.get_bind().dialect.name or "").lower()
        except Exception:
            dialect = ""
        if dialect == "postgresql":
            return func.regexp_replace(
                func.upper(func.coalesce(column, "")), r"[^A-Z0-9-]", "", "g"
            )

        expr = func.upper(func.coalesce(column, ""))
        for token in (
            " ",
            "-",
            "/",
            ".",
            "_",
            "(",
            ")",
            ":",
            ",",
            ";",
            "\\",
            "#",
            "[",
            "]",
            "{",
            "}",
            "+",
        ):
            expr = func.replace(expr, token, "")
        return expr

    @classmethod
    def find_by_reference(
        cls,
        reference: str | None,
        *,
        allow_normalized: bool = False,
    ) -> Matter | None:
        ref = (reference or "").strip()
        if not ref:
            return None

        matter = cls.active_query().filter(cls.reference_filter(ref)).first()
        if matter or not allow_normalized:
            return matter

        norm_ref = normalize_reference(ref)
        if len(norm_ref) <= 5:
            return None

        norm_refs = [norm_ref]
        for variant in reference_padding_variants(norm_ref):
            if variant not in norm_refs:
                norm_refs.append(variant)

        rows = (
            cls.active_query()
            .filter(
                or_(
                    cls._normalized_reference_expr(Matter.our_ref).in_(norm_refs),
                    cls._normalized_reference_expr(Matter.old_our_ref).in_(norm_refs),
                    cls._normalized_reference_expr(Matter.your_ref).in_(norm_refs),
                )
            )
            .limit(3)
            .all()
        )
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            logger.warning(
                "Ambiguous normalized Matter reference match ref=%s candidates=%s",
                ref,
                ",".join(str(getattr(m, "matter_id", "")) for m in rows),
            )
        return None

    @classmethod
    def resolve_matter_id_for_case_ref(cls, case_ref: str | None) -> str | None:
        matter = cls.find_by_reference(case_ref)
        return str(matter.matter_id) if matter else None

    @classmethod
    def match_references(cls, references: Iterable[str | None]) -> list[MatterReferenceMatch]:
        out: list[MatterReferenceMatch] = []
        seen: set[tuple[str, str, str, bool]] = set()
        for reference in references:
            ref = (reference or "").strip()
            if not ref:
                continue
            matter = cls.find_by_reference(ref, allow_normalized=True)
            if not matter:
                continue
            normalized = ref not in {
                (getattr(matter, "our_ref", None) or "").strip(),
                (getattr(matter, "old_our_ref", None) or "").strip(),
                (getattr(matter, "your_ref", None) or "").strip(),
            }
            matched_field = "our_ref"
            matched_value = ref
            for field in cls.REFERENCE_FIELDS:
                raw_value = (getattr(matter, field, None) or "").strip()
                if raw_value == ref or normalize_reference(raw_value) == normalize_reference(ref):
                    matched_field = field
                    matched_value = raw_value or ref
                    break
            key = (str(matter.matter_id), matched_field, matched_value, normalized)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                MatterReferenceMatch(
                    matter_id=str(matter.matter_id),
                    matched_field=matched_field,
                    matched_value=matched_value,
                    normalized=normalized,
                )
            )
        return out
