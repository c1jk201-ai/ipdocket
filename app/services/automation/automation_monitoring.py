from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from flask import current_app

from app.extensions import db
from app.models.ip_records import AutomationChangeSet, EmailMessage, ExtractionResult, IngestionRun
from app.models.system_config import SystemConfig
from app.services.automation.review_feedback import collect_doc_type_feedback_metrics
from app.services.core.config_service import ConfigService
from app.services.mail.foreign_email_pipeline import validate_extraction
from app.utils.error_logging import report_swallowed_exception


def _get_config_int(key: str, default: int) -> int:
    try:
        value = ConfigService.get_int(key, default)
        if value is not None:
            return int(value)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="automation_monitoring._get_config_int",
            log_key=f"automation_monitoring._get_config_int.{key}",
            log_window_seconds=300,
        )
    return int(default)


def _get_config_float(key: str, default: float) -> float:
    try:
        raw = ConfigService.get_raw(key, default, allow_blank=False)
        if raw is not None:
            return float(str(raw).strip())
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="automation_monitoring._get_config_float",
            log_key=f"automation_monitoring._get_config_float.{key}",
            log_window_seconds=300,
        )
    return float(default)


def _get_config_bool(key: str, default: bool) -> bool:
    try:
        return bool(ConfigService.get_bool(key, default))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="automation_monitoring._get_config_bool",
            log_key=f"automation_monitoring._get_config_bool.{key}",
            log_window_seconds=300,
        )
    return bool(default)


def _get_config_list(key: str, default: str) -> list[str]:
    try:
        raw = ConfigService.get_str(key, default, allow_blank=True)
    except Exception:
        raw = default
    if raw is None:
        return []
    parts = [p.strip() for p in str(raw).split(",")]
    return [p for p in parts if p]


def _alert_code(alert: str) -> str:
    return str(alert or "").split(":", 1)[0].strip()


def _desired_automation_override(alerts: list[str]) -> str:
    if not alerts:
        return ""
    alert_codes = {_alert_code(alert) for alert in alerts}
    alert_codes.discard("")
    critical = set(
        _get_config_list(
            "FOREIGN_EMAIL_DRIFT_CRITICAL_ALERTS",
            "error_rate_high,parsing_fail_rate_high,missing_evidence_rate_high",
        )
    )
    if alert_codes & critical:
        return "HUMAN_REQUIRED"

    draft_alerts = set(_get_config_list("FOREIGN_EMAIL_DRIFT_DRAFT_ALERTS", ""))
    if alert_codes & draft_alerts:
        return "AUTO_DRAFT"

    return ""


def _apply_automation_override(alerts: list[str]) -> str:
    enabled = _get_config_bool("FOREIGN_EMAIL_DRIFT_AUTO_DOWNGRADE_ENABLED", True)
    if not enabled:
        return ""

    desired = _desired_automation_override(alerts)
    current = SystemConfig.get_config("FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE", "") or ""
    if (current or "") != (desired or ""):
        SystemConfig.set_config("FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE", desired or "")
        db.session.commit()
        ConfigService.clear_cache()
    return desired or ""


def _get_warnings(structured_json: dict | None) -> list[str]:
    if not structured_json:
        return []
    warnings = structured_json.get("warnings") or []
    if isinstance(warnings, list):
        return [str(w) for w in warnings if w is not None]
    return []


def _warning_hit(warnings: list[str], needle: str) -> bool:
    return any(needle in w for w in warnings)


def collect_automation_metrics(*, window_days: int | None = None) -> dict:
    days = window_days or _get_config_int("FOREIGN_EMAIL_DRIFT_WINDOW_DAYS", 7)
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        db.session.query(IngestionRun, EmailMessage, ExtractionResult, AutomationChangeSet)
        .join(EmailMessage, EmailMessage.id == IngestionRun.email_id)
        .outerjoin(ExtractionResult, ExtractionResult.run_id == IngestionRun.id)
        .outerjoin(AutomationChangeSet, AutomationChangeSet.run_id == IngestionRun.id)
        .filter(IngestionRun.started_at >= since)
        .filter(~IngestionRun.status.like("SHADOW%"))
        .all()
    )

    total = len(rows)
    error_count = 0
    review_count = 0
    ready_count = 0
    extracted_count = 0

    warning_counts = {
        "missing_evidence": 0,
        "parsing_failed": 0,
        "invalid_date": 0,
        "missing_match": 0,
    }
    level_counts = {"AUTO_APPLY": 0, "AUTO_DRAFT": 0, "HUMAN_REQUIRED": 0}
    confidence_sums = {"overall": 0.0, "deadlines": 0.0, "annuities": 0.0}
    confidence_counts = {"overall": 0, "deadlines": 0, "annuities": 0}
    tokens_in = 0
    tokens_out = 0
    cost_sum = 0.0
    cost_count = 0

    for run, email, extraction, change_set in rows:
        if (run.status or "").upper() == "ERROR" or run.error_code:
            error_count += 1
        if (run.status or "").upper() in ("EXTRACTED", "APPLIED"):
            extracted_count += 1
        status = (email.processing_status or "").upper()
        if status == "REVIEW":
            review_count += 1
        elif status == "READY":
            ready_count += 1

        if run.tokens_in:
            tokens_in += int(run.tokens_in or 0)
        if run.tokens_out:
            tokens_out += int(run.tokens_out or 0)
        if run.cost_estimate is not None:
            cost_sum += float(run.cost_estimate or 0)
            cost_count += 1

        structured_json = extraction.structured_json if extraction else {}
        warnings = _get_warnings(structured_json)
        if _warning_hit(warnings, "missing evidence"):
            warning_counts["missing_evidence"] += 1
        if _warning_hit(warnings, "parsing failed"):
            warning_counts["parsing_failed"] += 1
        if _warning_hit(warnings, "invalid_date"):
            warning_counts["invalid_date"] += 1
        if _warning_hit(warnings, "Matter match missing"):
            warning_counts["missing_match"] += 1

        if extraction:
            matched = bool(change_set and change_set.matter_id) or bool(
                ((structured_json.get("case_target") or {}).get("matter_id"))
            )
            validation = validate_extraction(structured_json, has_matter_match=matched)
            level_counts[validation.level] = level_counts.get(validation.level, 0) + 1
        else:
            # M-8 fix: extraction  run( Failed/Error)
            # level_counts HUMAN_REQUIRED  rate   .
            level_counts["HUMAN_REQUIRED"] = level_counts.get("HUMAN_REQUIRED", 0) + 1

        confidence = (structured_json.get("confidence") or {}) if structured_json else {}
        for key in ("overall", "deadlines", "annuities"):
            val = confidence.get(key)
            try:
                if val is not None:
                    confidence_sums[key] += float(val)
                    confidence_counts[key] += 1
            except Exception:
                continue

    def _rate(count: int, denom: int) -> float:
        return round(count / denom, 4) if denom else 0.0

    confidence_avg = {
        key: (confidence_sums[key] / confidence_counts[key]) if confidence_counts[key] else None
        for key in confidence_sums
    }

    metrics = {
        "window_days": days,
        "total_runs": total,
        "error_rate": _rate(error_count, total),
        "review_rate": _rate(review_count, total),
        "ready_rate": _rate(ready_count, total),
        "extracted_rate": _rate(extracted_count, total),
        "warning_rates": {key: _rate(val, total) for key, val in warning_counts.items()},
        "validation_levels": level_counts,
        "confidence_avg": confidence_avg,
        "tokens": {"in": tokens_in, "out": tokens_out},
        "cost": {
            "total": round(cost_sum, 4),
            "avg": round(cost_sum / cost_count, 4) if cost_count else 0.0,
        },
    }
    metrics["feedback"] = collect_doc_type_feedback_metrics(window_days=days)
    return metrics


def check_automation_drift(metrics: dict[str, Any]) -> list[str]:
    alerts: list[str] = []

    error_rate = float(metrics.get("error_rate") or 0.0)
    review_rate = float(metrics.get("review_rate") or 0.0)
    warning_rates = metrics.get("warning_rates") or {}

    if error_rate > _get_config_float("FOREIGN_EMAIL_DRIFT_MAX_ERROR_RATE", 0.1):
        alerts.append(f"error_rate_high:{error_rate:.2f}")
    if review_rate > _get_config_float("FOREIGN_EMAIL_DRIFT_MAX_REVIEW_RATE", 0.7):
        alerts.append(f"review_rate_high:{review_rate:.2f}")

    missing_evidence = float(warning_rates.get("missing_evidence") or 0.0)
    if missing_evidence > _get_config_float("FOREIGN_EMAIL_DRIFT_MAX_MISSING_EVIDENCE_RATE", 0.2):
        alerts.append(f"missing_evidence_rate_high:{missing_evidence:.2f}")

    parsing_failed = float(warning_rates.get("parsing_failed") or 0.0)
    if parsing_failed > _get_config_float("FOREIGN_EMAIL_DRIFT_MAX_PARSING_FAIL_RATE", 0.1):
        alerts.append(f"parsing_fail_rate_high:{parsing_failed:.2f}")

    missing_match = float(warning_rates.get("missing_match") or 0.0)
    if missing_match > _get_config_float("FOREIGN_EMAIL_DRIFT_MAX_MATCH_MISS_RATE", 0.2):
        alerts.append(f"missing_match_rate_high:{missing_match:.2f}")

    return alerts


def run_automation_drift_check(*, window_days: int | None = None) -> dict:
    metrics = collect_automation_metrics(window_days=window_days)
    alerts = check_automation_drift(metrics)
    override = _apply_automation_override(alerts)
    if alerts:
        current_app.logger.warning("Foreign email automation drift alerts: %s", alerts)
    else:
        current_app.logger.info("Foreign email automation drift check ok")
    return {"metrics": metrics, "alerts": alerts, "automation_override": override}


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    base = (month - 1) + delta
    year += base // 12
    month = (base % 12) + 1
    return year, month


def collect_monthly_automation_metrics(*, months: int = 12) -> list[dict]:
    months = int(months or 12)
    months = max(1, min(months, 36))

    now = datetime.utcnow()
    start_year, start_month = _shift_month(now.year, now.month, -(months - 1))
    start_at = datetime(start_year, start_month, 1)

    rows = (
        db.session.query(IngestionRun, EmailMessage)
        .join(EmailMessage, EmailMessage.id == IngestionRun.email_id)
        .filter(IngestionRun.started_at >= start_at)
        .filter(~IngestionRun.status.like("SHADOW%"))
        .all()
    )

    buckets: dict[str, dict[str, float]] = {}
    for run, email in rows:
        if not run.started_at:
            continue
        key = run.started_at.strftime("%Y-%m")
        bucket = buckets.setdefault(
            key,
            {
                "total": 0,
                "success": 0,
                "review": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_total": 0.0,
            },
        )
        bucket["total"] += 1
        if (run.status or "").upper() in ("EXTRACTED", "APPLIED") and not run.error_code:
            bucket["success"] += 1
        if (email.processing_status or "").upper() == "REVIEW":
            bucket["review"] += 1
        if run.tokens_in:
            bucket["tokens_in"] += int(run.tokens_in or 0)
        if run.tokens_out:
            bucket["tokens_out"] += int(run.tokens_out or 0)
        if run.cost_estimate is not None:
            bucket["cost_total"] += float(run.cost_estimate or 0)

    out = []
    for idx in range(months):
        year, month = _shift_month(start_year, start_month, idx)
        key = f"{year:04d}-{month:02d}"
        bucket = buckets.get(key, {})
        total = int(bucket.get("total", 0))
        success = int(bucket.get("success", 0))
        review = int(bucket.get("review", 0))
        success_rate = round(success / total, 4) if total else 0.0
        review_rate = round(review / total, 4) if total else 0.0
        out.append(
            {
                "month": key,
                "total_runs": total,
                "success_rate": success_rate,
                "review_rate": review_rate,
                "tokens_in": int(bucket.get("tokens_in", 0)),
                "tokens_out": int(bucket.get("tokens_out", 0)),
                "cost_total": round(float(bucket.get("cost_total", 0.0)), 4),
            }
        )
    return out
