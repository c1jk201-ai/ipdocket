from flask import current_app, request
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import Matter
from app.services.case.case_kind import _normalize_case_division
from app.services.case.case_numbering import NextOurRefError, generate_next_our_ref
from app.utils.api_response import api_response
from app.utils.case_dto import matter_summary
from app.utils.permissions import can_access_matter


@bp.get("/api/next_our_ref")
@login_required
def api_next_our_ref():
    try:
        division = (request.args.get("division") or "").strip().upper()
        matter_type = (request.args.get("type") or "").strip().upper()
        country = (request.args.get("country") or "").strip().upper()
        # Preview endpoint: do not reserve counter values here.
        # Reserving on button click causes gaps when users cancel without saving.
        next_ref = generate_next_our_ref(
            division=division,
            matter_type=matter_type,
            country=country,
        )
        return api_response(value={"our_ref": next_ref}, legacy={"our_ref": next_ref})
    except NextOurRefError as exc:
        try:
            db.session.rollback()
        except Exception:
            current_app.logger.warning(
                "db.session.rollback failed (api_next_our_ref:NextOurRefError)", exc_info=True
            )
        return api_response(
            ok=False,
            error=exc.code,
            message=exc.message,
            status=exc.status,
        )
    except Exception:
        current_app.logger.exception("Failed to generate next our_ref")
        try:
            db.session.rollback()
        except Exception:
            current_app.logger.warning(
                "db.session.rollback failed (api_next_our_ref:Exception)", exc_info=True
            )
        return api_response(
            ok=False, error="internal_error", message="Auto In Progress Error .", status=500
        )


@bp.get("/api/check_family_candidate")
@login_required
def api_check_family_candidate():
    try:
        our_ref = (request.args.get("our_ref") or "").strip()
        ignore_id = (request.args.get("ignore_id") or "").strip()
        title_hint = (request.args.get("title") or "").strip()
        requested_division = _normalize_case_division(request.args.get("division"))

        if not our_ref:
            return api_response(value=None)

        # Only allow candidates with the exact same YY+TYPE+SEQ prefix
        # (e.g. 26PO0105US -> base 26PO0105). Different YY must never auto-suggest.

        import re
        from difflib import SequenceMatcher

        def _infer_division_from_ref(value: str) -> str:
            ref = str(value or "").strip().upper()
            if not ref:
                return ""
            if re.match(r"^\d{2}PD\d{4}PCT$", ref):
                return "OUT"
            if len(ref) >= 4 and ref[:2].isdigit():
                code = ref[2:4]
                if len(code) == 2:
                    div = code[1:2]
                    if div == "D":
                        return "DOM"
                    if div == "I":
                        return "INC"
                    if div == "O":
                        return "OUT"
            return ""

        def _resolve_division(matter: Matter | None) -> str:
            if not matter:
                return ""
            return _normalize_case_division(
                getattr(matter, "right_group", None)
            ) or _infer_division_from_ref(getattr(matter, "our_ref", None))

        def _normalize_title(value: str) -> str:
            raw = str(value or "").strip().lower()
            if not raw:
                return ""
            raw = re.sub(r"[^0-9a-z-]+", " ", raw)
            return re.sub(r"\s+", " ", raw).strip()

        def _is_informative_title(value: str) -> bool:
            norm = _normalize_title(value)
            if not norm:
                return False
            if len(norm.replace(" ", "")) < 6:
                return False
            return norm not in {"Matter", "Patent", "Utility model", "Design", "Trademark", "Filing", "case"}

        def _title_similarity(a: str, b: str) -> float:
            na = _normalize_title(a)
            nb = _normalize_title(b)
            if not na or not nb:
                return 0.0
            ta = {tok for tok in na.split(" ") if len(tok) >= 2}
            tb = {tok for tok in nb.split(" ") if len(tok) >= 2}
            jaccard = 0.0
            if ta and tb:
                uni = ta | tb
                if uni:
                    jaccard = len(ta & tb) / len(uni)
            seq = SequenceMatcher(None, na, nb).ratio()
            containment = 0.0
            short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
            if short and short in long_:
                containment = len(short) / len(long_)
            return max(jaccard, seq, containment)

        def _is_title_compatible(candidate_title: str) -> bool:
            if not _is_informative_title(title_hint):
                return True
            if not _is_informative_title(candidate_title):
                return False
            return _title_similarity(title_hint, candidate_title) >= 0.55

        m = re.match(r"^(\d{2}[A-Z]{2}\d{4})", our_ref, re.IGNORECASE)
        if not m:
            return api_response(value=None)

        source_division = requested_division or _infer_division_from_ref(our_ref)
        if source_division != "OUT":
            return api_response(value=None)

        base_ref = m.group(1).upper()

        # 1) Same-year base match
        q = Matter.query.filter(
            func.upper(func.coalesce(Matter.our_ref, "")).like(f"{base_ref}%")
        ).filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
        if ignore_id:
            q = q.filter(Matter.matter_id != ignore_id)

        candidates = q.limit(20).all()
        # Filter strictly by regex base to avoid partial prefix overlap

        valid_candidate = None
        for cand in candidates:
            if not cand.our_ref:
                continue
            if cand.our_ref.strip().upper() == our_ref.upper():
                continue
            if _resolve_division(cand) != "OUT":
                continue
            if not can_access_matter(current_user, str(cand.matter_id), action="view"):
                continue
            if not _is_title_compatible((cand.right_name or "").strip()):
                continue
            # Verify base match
            cm = re.match(r"^(\d{2}[A-Z]{2}\d{4})", cand.our_ref, re.IGNORECASE)
            if cm and cm.group(1).upper() == base_ref:
                valid_candidate = cand
                break

        if valid_candidate:
            payload = {
                "matter_id": valid_candidate.matter_id,
                "our_ref": valid_candidate.our_ref,
                "title": (valid_candidate.right_name or ""),
                "matter": matter_summary(valid_candidate),
            }
            return api_response(value=payload, legacy=payload)

        return api_response(value=None)
    except Exception:
        current_app.logger.exception("Failed to check family candidate")
        return api_response(
            ok=False, error="internal_error", message="Family  Search Failed", status=500
        )
