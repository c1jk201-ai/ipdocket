from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import current_app

from app.extensions import db
from app.models.client import Client
from app.models.operation import Operation
from app.ops.durable_queue import build_queue_from_app
from app.services.client.client_tagging import build_client_search_tags_text
from app.services.client.name_normalization import normalize_client_name
from app.services.core.llm_runtime import get_openai_api_key
from app.utils.error_logging import report_swallowed_exception


def enqueue_deferred_task(
    task: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 3,
    dedupe_key: str | None = None,
    source_event_id: str | None = None,
    idempotency_scope: str | None = None,
) -> int | None:
    job = build_queue_from_app(current_app).enqueue(
        task,
        payload,
        queue="deferred",
        max_attempts=max_attempts,
        dedupe_key=dedupe_key,
        source_event_id=source_event_id,
        idempotency_scope=idempotency_scope or task,
    )
    try:
        return int(job.id)
    except Exception:
        return None


def _crm_client_search_values(client: Client) -> list[Any]:
    extra = client.extra if isinstance(client.extra, dict) else {}
    applicant_codes = extra.get("applicant_codes") or []
    if not isinstance(applicant_codes, (list, tuple, set)):
        applicant_codes = [applicant_codes]
    return [
        client.name,
        client.biz_company_name,
        extra.get("name_en"),
        extra.get("tax_company_name"),
        extra.get("client_code"),
        *applicant_codes,
        getattr(client, "registration_number", None),
        getattr(client, "biz_reg_number", None),
        getattr(client, "phone", None),
    ]


def set_crm_client_search_tags_fast(client: Client) -> None:
    client.search_tags = (
        build_client_search_tags_text(_crm_client_search_values(client), use_llm=False) or None
    )


def enqueue_crm_client_post_save(client_id: int | str) -> int | None:
    cid = int(client_id)
    return enqueue_deferred_task(
        "client.crm_post_save",
        {"client_id": cid},
        max_attempts=3,
        dedupe_key=f"client.crm_post_save:{cid}",
        source_event_id=str(cid),
    )


def refresh_crm_client_search_tags(client_id: int | str) -> None:
    client = db.session.get(Client, int(client_id))
    if not client:
        return
    api_key = get_openai_api_key(allow_legacy=False) or None
    client.search_tags = (
        build_client_search_tags_text(
            _crm_client_search_values(client),
            api_key=api_key,
            use_llm=bool(api_key),
        )
        or None
    )
    db.session.commit()


def _invoice_sync_enabled() -> bool:
    from app.services.billing.db_core import unified_clients_enabled

    return bool(
        current_app.config.get("INVOICEAPP_CLIENT_SYNC_ENABLED")
        and current_app.config.get("INVOICEAPP_INTEGRATED")
        and not unified_clients_enabled()
    )


def _run_invoice_client_sync() -> None:
    if not _invoice_sync_enabled():
        return
    try:
        from client_sync_sqlite import sync_clients_bidirectional
    except Exception:
        return
    try:
        sync_clients_bidirectional(current_app.config.get("DB_PATH") or "")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="client.background_jobs.run_invoice_client_sync",
            log_key="client.background_jobs.run_invoice_client_sync",
            log_window_seconds=300,
        )


def invoice_client_search_tags_fast(values: list[Any]) -> str:
    return build_client_search_tags_text(values, use_llm=False)


def enqueue_invoice_client_post_save(client_id: int | str) -> int | None:
    cid = int(client_id)
    return enqueue_deferred_task(
        "client.invoice_post_save",
        {"client_id": cid},
        max_attempts=3,
        dedupe_key=f"client.invoice_post_save:{cid}",
        source_event_id=str(cid),
    )


def refresh_invoice_client_search_tags(client_id: int | str) -> None:
    from app.services.billing.db_core import get_db

    cid = int(client_id)
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        if not row:
            return

        def get_value(key: str) -> Any:
            try:
                return row[key]
            except Exception:
                try:
                    mapping = getattr(row, "_mapping", None)
                    if mapping is not None:
                        return mapping.get(key)
                except Exception:
                    return None
            return None

        values = [
            get_value("name"),
            get_value("biz_company_name"),
            get_value("biz_reg_number"),
            get_value("biz_corp_registration_number"),
            get_value("phone"),
        ]
        api_key = get_openai_api_key(allow_legacy=False) or None
        tags = build_client_search_tags_text(values, api_key=api_key, use_llm=bool(api_key))
        conn.execute("UPDATE clients SET search_tags=? WHERE id=?", (tags or None, cid))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client.background_jobs.refresh_invoice_client_search_tags.close",
                log_key="client.background_jobs.refresh_invoice_client_search_tags.close",
                log_window_seconds=300,
            )


def run_crm_client_post_save(payload: dict[str, Any]) -> None:
    refresh_crm_client_search_tags(payload.get("client_id"))


def run_invoice_client_post_save(payload: dict[str, Any]) -> None:
    refresh_invoice_client_search_tags(payload.get("client_id"))
    _run_invoice_client_sync()


def create_customer_llm_parse_operation(*, actor_id: int | None, email_text: str) -> Operation:
    op = Operation(
        request_id=None,
        actor_id=actor_id,
        action="client.parse_customer_llm",
        risk_level="LOW",
        status="queued",
        targets_json={},
        summary_json={"queued_at": datetime.utcnow().isoformat(), "input_chars": len(email_text)},
        created_at=datetime.utcnow(),
    )
    db.session.add(op)
    db.session.flush()
    return op


def enqueue_customer_llm_parse(operation_id: int, email_text: str) -> int | None:
    op_id = int(operation_id)
    return enqueue_deferred_task(
        "client.parse_customer_llm",
        {"operation_id": op_id, "email_text": email_text},
        max_attempts=2,
        dedupe_key=f"client.parse_customer_llm:{op_id}",
        source_event_id=str(op_id),
    )


def parse_customer_llm_background(operation_id: int | str, email_text: str) -> None:
    from app.services.billing.llm_parser import parse_customer_from_text

    op = db.session.get(Operation, int(operation_id))
    if not op:
        return
    try:
        op.status = "running"
        op.summary_json = dict(op.summary_json or {}, started_at=datetime.utcnow().isoformat())
        db.session.commit()

        api_key = get_openai_api_key(allow_legacy=False)
        if not api_key:
            raise RuntimeError("OpenAI API  Settings .")

        customer_data = parse_customer_from_text((email_text or "").strip(), api_key)
        if not isinstance(customer_data, dict):
            customer_data = {}
        customer_data.update(normalize_client_name(customer_data.get("name"), api_key=api_key))

        op.status = "succeeded"
        op.summary_json = {
            "customer": customer_data,
            "finished_at": datetime.utcnow().isoformat(),
        }
        op.applied_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        op = db.session.get(Operation, int(operation_id))
        if op:
            op.status = "failed"
            op.error_text = str(exc)
            op.summary_json = dict(
                op.summary_json or {},
                finished_at=datetime.utcnow().isoformat(),
            )
            db.session.commit()
        raise


def run_customer_llm_parse(payload: dict[str, Any]) -> None:
    parse_customer_llm_background(payload.get("operation_id"), payload.get("email_text") or "")
