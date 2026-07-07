from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from flask import has_app_context

from app.extensions import db
from app.models.ip_records import (
    AutomationChangeSet,
    AutomationFieldFeedback,
    AutomationReviewFeedback,
    ExtractionResult,
)
from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception

LABEL_ACCEPTED = "accepted"
LABEL_CORRECTED = "corrected"
LABEL_REJECTED = "rejected"

ACTION_APPLY = "apply"
ACTION_REJECT = "reject"
ACTION_UPDATE_PAYLOAD = "update_payload"
ACTION_SELECT_MATTER = "select_matter"

_DEFAULT_DOC_TYPE = "UNKNOWN"
_FIELD_FEEDBACK_ROOTS = {
    "case_target",
    "doc",
    "route",
    "params",
    "identifiers",
    "events",
    "dockets",
    "annuities",
}
_FIELD_FEEDBACK_MAX_FIELDS = 400
_FIELD_VALUE_MAX_CHARS = 500


@dataclass(frozen=True)
class AutoApplyGateDecision:
    allowed: bool
    reason: str = ""
    metrics: dict[str, Any] | None = None


def _latest_extraction(run_id: str | None) -> ExtractionResult | None:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    return (
        ExtractionResult.query.filter_by(run_id=rid)
        .order_by(ExtractionResult.created_at.desc(), ExtractionResult.id.desc())
        .first()
    )


def _normalize_doc_type(value: Any) -> str:
    raw = str(value or "").strip()
    return raw or _DEFAULT_DOC_TYPE


def _structured_json(extraction: ExtractionResult | None) -> dict[str, Any]:
    payload = extraction.structured_json if extraction else {}
    return payload if isinstance(payload, dict) else {}


def _doc_type_from(
    *,
    extraction: ExtractionResult | None,
    structured_json: dict[str, Any] | None = None,
) -> str:
    if extraction and extraction.doc_type:
        return _normalize_doc_type(extraction.doc_type)
    payload = structured_json if isinstance(structured_json, dict) else _structured_json(extraction)
    doc = payload.get("doc") if isinstance(payload, dict) else {}
    return _normalize_doc_type((doc or {}).get("doc_type") if isinstance(doc, dict) else None)


def _confidence_from(
    extraction: ExtractionResult | None, structured_json: dict[str, Any]
) -> float | None:
    if extraction and extraction.overall_confidence is not None:
        try:
            return float(extraction.overall_confidence)
        except Exception:
            return None
    confidence = structured_json.get("confidence") if isinstance(structured_json, dict) else {}
    if not isinstance(confidence, dict):
        return None
    try:
        value = confidence.get("overall")
        return float(value) if value is not None else None
    except Exception:
        return None


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _field_value_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > _FIELD_VALUE_MAX_CHARS:
        return text[: _FIELD_VALUE_MAX_CHARS - 3] + "..."
    return text


def _path_join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _flatten_fields(value: Any, *, prefix: str = "") -> dict[str, Any]:
    if not prefix:
        if not isinstance(value, dict):
            return {}
        flattened: dict[str, Any] = {}
        for key in _FIELD_FEEDBACK_ROOTS:
            if key in value:
                flattened.update(_flatten_fields(value.get(key), prefix=key))
        return flattened

    if _is_scalar(value):
        return {prefix: value}
    if isinstance(value, dict):
        flattened = {}
        for key, child in value.items():
            if str(key).startswith("_"):
                continue
            flattened.update(_flatten_fields(child, prefix=_path_join(prefix, str(key))))
        return flattened
    if isinstance(value, list):
        flattened = {}
        for idx, child in enumerate(value):
            flattened.update(_flatten_fields(child, prefix=f"{prefix}[{idx}]"))
        return flattened
    return {prefix: str(value)}


def _evidence_paths(structured_json: dict[str, Any]) -> set[str]:
    evidence_map = structured_json.get("evidence_map")
    if not isinstance(evidence_map, dict):
        return set()
    out = set()
    for key in evidence_map.keys():
        raw = str(key or "").strip()
        if not raw:
            continue
        out.add(raw)
        out.add(raw.replace("[", ".").replace("]", ""))
    return out


def _path_has_evidence(path: str, evidence_paths: set[str]) -> bool:
    if not evidence_paths:
        return False
    normalized = path.replace("[", ".").replace("]", "")
    if path in evidence_paths or normalized in evidence_paths:
        return True
    return any(
        ep and (normalized.startswith(ep + ".") or ep.startswith(normalized + "."))
        for ep in evidence_paths
    )


def _field_feedback_candidates(
    *,
    action: str,
    label: str,
    structured_json: dict[str, Any],
    before_json: dict[str, Any] | None,
    after_json: dict[str, Any] | None,
    details: dict[str, Any] | None,
) -> list[tuple[str, str, Any, Any]]:
    before = before_json if isinstance(before_json, dict) else None
    after = after_json if isinstance(after_json, dict) else None
    action_key = str(action or "").strip()
    label_key = str(label or "").strip()

    if label_key == LABEL_ACCEPTED and action_key == ACTION_APPLY:
        after_fields = _flatten_fields(after or structured_json)
        return [
            (path, LABEL_ACCEPTED, None, value)
            for path, value in after_fields.items()
            if value not in (None, "", [], {})
        ][:_FIELD_FEEDBACK_MAX_FIELDS]

    if label_key == LABEL_REJECTED or action_key == ACTION_REJECT:
        before_fields = _flatten_fields(before or structured_json)
        return [
            (path, LABEL_REJECTED, value, None)
            for path, value in before_fields.items()
            if value not in (None, "", [], {})
        ][:_FIELD_FEEDBACK_MAX_FIELDS]

    before_fields = _flatten_fields(before or {})
    after_fields = _flatten_fields(after or structured_json or {})
    paths = sorted(set(before_fields.keys()) | set(after_fields.keys()))
    changed = [
        (path, LABEL_CORRECTED, before_fields.get(path), after_fields.get(path))
        for path in paths
        if before_fields.get(path) != after_fields.get(path)
    ]

    remove_paths = []
    if isinstance(details, dict):
        raw_remove_paths = details.get("remove_paths")
        if isinstance(raw_remove_paths, list):
            remove_paths = [str(path).strip() for path in raw_remove_paths if str(path).strip()]
    for path in remove_paths:
        if path not in {row[0] for row in changed}:
            changed.append((path, LABEL_REJECTED, before_fields.get(path), None))

    return changed[:_FIELD_FEEDBACK_MAX_FIELDS]


def _record_field_feedback_rows(
    *,
    feedback: AutomationReviewFeedback,
    structured_json: dict[str, Any],
    before_json: dict[str, Any] | None,
    after_json: dict[str, Any] | None,
    details: dict[str, Any] | None,
) -> None:
    if not feedback.id:
        return
    evidence = _evidence_paths(structured_json)
    confidence = _confidence_from(None, structured_json)
    for path, field_label, before_value, after_value in _field_feedback_candidates(
        action=feedback.action,
        label=feedback.label,
        structured_json=structured_json,
        before_json=before_json,
        after_json=after_json,
        details=details,
    ):
        db.session.add(
            AutomationFieldFeedback(
                feedback_id=str(feedback.id),
                run_id=str(feedback.run_id or ""),
                extraction_result_id=feedback.extraction_result_id,
                change_set_id=feedback.change_set_id,
                matter_id=feedback.matter_id,
                doc_type=feedback.doc_type,
                action=feedback.action,
                label=field_label,
                field_path=path,
                reviewer_id=feedback.reviewer_id,
                before_value=_field_value_text(before_value),
                after_value=_field_value_text(after_value),
                confidence=confidence,
                evidence_present=_path_has_evidence(path, evidence),
                details={"parent_label": feedback.label},
            )
        )


def _validation_level(structured_json: dict[str, Any], has_match: bool) -> str:
    try:
        from app.services.mail.foreign_email_pipeline import validate_extraction

        return str(validate_extraction(structured_json, has_matter_match=has_match).level or "")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="automation.review_feedback.validation_level",
            log_key="automation.review_feedback.validation_level",
            log_window_seconds=300,
        )
        return ""


def record_automation_feedback(
    *,
    run_id: str,
    action: str,
    label: str,
    reviewer_id: str | None = None,
    reason: str | None = None,
    extraction: ExtractionResult | None = None,
    change_set: AutomationChangeSet | None = None,
    before_json: dict[str, Any] | None = None,
    after_json: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> AutomationReviewFeedback | None:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    extraction = extraction or _latest_extraction(rid)
    structured = after_json or before_json or _structured_json(extraction)
    if not isinstance(structured, dict):
        structured = {}

    matter_id = None
    if change_set and getattr(change_set, "matter_id", None):
        matter_id = str(change_set.matter_id)
    else:
        case_target = structured.get("case_target") if isinstance(structured, dict) else {}
        if isinstance(case_target, dict) and case_target.get("matter_id"):
            matter_id = str(case_target.get("matter_id"))

    feedback = AutomationReviewFeedback(
        id=uuid4().hex,
        run_id=rid,
        extraction_result_id=str(extraction.id) if extraction and extraction.id else None,
        change_set_id=str(change_set.id) if change_set and change_set.id else None,
        matter_id=matter_id,
        doc_type=_doc_type_from(extraction=extraction, structured_json=structured),
        action=str(action or "").strip() or "unknown",
        label=str(label or "").strip() or LABEL_CORRECTED,
        reason=str(reason or "").strip() or None,
        reviewer_id=str(reviewer_id or "").strip() or None,
        automation_level=_validation_level(structured, bool(matter_id)),
        confidence_overall=_confidence_from(extraction, structured),
        before_json=before_json if isinstance(before_json, dict) else None,
        after_json=after_json if isinstance(after_json, dict) else None,
        details=details if isinstance(details, dict) else None,
    )
    db.session.add(feedback)
    _record_field_feedback_rows(
        feedback=feedback,
        structured_json=structured,
        before_json=before_json if isinstance(before_json, dict) else None,
        after_json=after_json if isinstance(after_json, dict) else None,
        details=details if isinstance(details, dict) else None,
    )
    return feedback


def record_feedback_best_effort(**kwargs: Any) -> None:
    try:
        record_automation_feedback(**kwargs)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="automation.review_feedback.record_feedback_best_effort",
            log_key="automation.review_feedback.record_feedback_best_effort",
            log_window_seconds=300,
        )


def _safe_rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def collect_doc_type_feedback_metrics(*, window_days: int | None = None) -> dict[str, Any]:
    days = ConfigService.get_int(
        "FOREIGN_EMAIL_AUTO_APPLY_FEEDBACK_WINDOW_DAYS",
        90 if window_days is None else window_days,
        min_value=1,
        max_value=3650,
    )
    if window_days is not None:
        days = max(1, int(window_days))
    since = datetime.utcnow() - timedelta(days=int(days or 90))

    rows = (
        AutomationReviewFeedback.query.filter(AutomationReviewFeedback.created_at >= since)
        .with_entities(
            AutomationReviewFeedback.doc_type,
            AutomationReviewFeedback.label,
            AutomationReviewFeedback.action,
        )
        .all()
    )

    buckets: dict[str, Counter[str]] = {}
    for doc_type, label, action in rows:
        key = _normalize_doc_type(doc_type)
        bucket = buckets.setdefault(key, Counter())
        normalized_label = str(label or "").strip() or "unknown"
        bucket[normalized_label] += 1
        bucket[f"action:{str(action or '').strip() or 'unknown'}"] += 1
        bucket["total"] += 1

    doc_types: dict[str, dict[str, Any]] = {}
    for doc_type, counts in buckets.items():
        accepted = int(counts.get(LABEL_ACCEPTED, 0))
        corrected = int(counts.get(LABEL_CORRECTED, 0))
        rejected = int(counts.get(LABEL_REJECTED, 0))
        reviewed = accepted + corrected + rejected
        # Recall needs a fully labelled golden set. Until that exists, this proxy treats
        # human corrections as missed correct outputs and gates promotion conservatively.
        recall_denominator = accepted + corrected
        doc_types[doc_type] = {
            "samples": int(counts.get("total", 0)),
            "reviewed_samples": reviewed,
            "accepted": accepted,
            "corrected": corrected,
            "rejected": rejected,
            "precision": _safe_rate(accepted, reviewed),
            "recall": _safe_rate(accepted, recall_denominator),
            "actions": {
                key.split(":", 1)[1]: int(value)
                for key, value in counts.items()
                if key.startswith("action:")
            },
        }

    field_rows = (
        AutomationFieldFeedback.query.filter(AutomationFieldFeedback.created_at >= since)
        .with_entities(
            AutomationFieldFeedback.doc_type,
            AutomationFieldFeedback.field_path,
            AutomationFieldFeedback.label,
            AutomationFieldFeedback.evidence_present,
        )
        .all()
    )
    field_buckets: dict[str, dict[str, Counter[str]]] = {}
    for doc_type, field_path, label, evidence_present in field_rows:
        doc_key = _normalize_doc_type(doc_type)
        path = str(field_path or "").strip()
        if not path:
            continue
        bucket = field_buckets.setdefault(doc_key, {}).setdefault(path, Counter())
        normalized_label = str(label or "").strip() or "unknown"
        bucket[normalized_label] += 1
        bucket["total"] += 1
        if not evidence_present:
            bucket["missing_evidence"] += 1

    suggestions: list[dict[str, Any]] = []
    for doc_type, fields in field_buckets.items():
        doc_metrics = doc_types.setdefault(
            doc_type,
            {
                "samples": 0,
                "reviewed_samples": 0,
                "accepted": 0,
                "corrected": 0,
                "rejected": 0,
                "precision": 0.0,
                "recall": 0.0,
                "actions": {},
            },
        )
        field_metrics: dict[str, dict[str, Any]] = {}
        for path, counts in fields.items():
            total = int(counts.get("total", 0))
            accepted = int(counts.get(LABEL_ACCEPTED, 0))
            corrected = int(counts.get(LABEL_CORRECTED, 0))
            rejected = int(counts.get(LABEL_REJECTED, 0))
            missing_evidence = int(counts.get("missing_evidence", 0))
            precision = _safe_rate(accepted, accepted + corrected + rejected)
            missing_evidence_rate = _safe_rate(missing_evidence, total)
            false_positive_rate = _safe_rate(rejected, accepted + corrected + rejected)
            row = {
                "samples": total,
                "accepted": accepted,
                "corrected": corrected,
                "rejected": rejected,
                "precision": precision,
                "false_positive_rate": false_positive_rate,
                "missing_evidence_rate": missing_evidence_rate,
            }
            field_metrics[path] = row
            if total >= 5 and precision < 0.9:
                suggestions.append(
                    {
                        "doc_type": doc_type,
                        "field_path": path,
                        "kind": "threshold_review",
                        "reason": "field_precision_below_target",
                        "precision": precision,
                        "samples": total,
                    }
                )
            if total >= 5 and missing_evidence_rate > 0.2:
                suggestions.append(
                    {
                        "doc_type": doc_type,
                        "field_path": path,
                        "kind": "evidence_rule_review",
                        "reason": "missing_evidence_rate_high",
                        "missing_evidence_rate": missing_evidence_rate,
                        "samples": total,
                    }
                )
        doc_metrics["fields"] = field_metrics

    return {
        "window_days": int(days or 90),
        "doc_types": doc_types,
        "suggestions": suggestions[:50],
    }


def auto_apply_gate_decision(structured_json: dict[str, Any]) -> AutoApplyGateDecision:
    if not has_app_context():
        return AutoApplyGateDecision(allowed=True)
    if not ConfigService.get_bool("FOREIGN_EMAIL_AUTO_APPLY_FEEDBACK_GATE_ENABLED", False):
        return AutoApplyGateDecision(allowed=True)

    doc_type = _doc_type_from(extraction=None, structured_json=structured_json)
    metrics = collect_doc_type_feedback_metrics()
    row = (metrics.get("doc_types") or {}).get(doc_type) or {}

    min_samples = ConfigService.get_int(
        "FOREIGN_EMAIL_AUTO_APPLY_FEEDBACK_MIN_SAMPLES",
        30,
        min_value=1,
        max_value=10000,
    )
    min_precision = float(
        ConfigService.get_str(
            "FOREIGN_EMAIL_AUTO_APPLY_FEEDBACK_MIN_PRECISION",
            "0.98",
            allow_blank=False,
        )
        or "0.98"
    )
    min_recall = float(
        ConfigService.get_str(
            "FOREIGN_EMAIL_AUTO_APPLY_FEEDBACK_MIN_RECALL",
            "0.95",
            allow_blank=False,
        )
        or "0.95"
    )

    samples = int(row.get("reviewed_samples") or 0)
    if samples < int(min_samples or 30):
        return AutoApplyGateDecision(
            allowed=False,
            reason=f"auto_apply_feedback_gate_insufficient_samples:{doc_type}:{samples}/{min_samples}",
            metrics=row,
        )

    precision = float(row.get("precision") or 0.0)
    if precision < min_precision:
        return AutoApplyGateDecision(
            allowed=False,
            reason=f"auto_apply_feedback_gate_precision_below:{doc_type}:{precision:.2f}<{min_precision:.2f}",
            metrics=row,
        )

    recall = float(row.get("recall") or 0.0)
    if recall < min_recall:
        return AutoApplyGateDecision(
            allowed=False,
            reason=f"auto_apply_feedback_gate_recall_below:{doc_type}:{recall:.2f}<{min_recall:.2f}",
            metrics=row,
        )

    return AutoApplyGateDecision(allowed=True, metrics=row)
