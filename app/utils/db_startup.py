import os
import re

from sqlalchemy import inspect

from app.extensions import db
from app.models.annuity_workflow_sync_dead_letter import AnnuityWorkflowSyncDeadLetter
from app.models.annuity_workflow_sync_queue import AnnuityWorkflowSyncQueue
from app.models.assets import FileAsset, MatterFileAsset
from app.models.audit_log import AuditLog
from app.models.backup_set import BackupSet
from app.models.case import Case
from app.models.case_audit_log import CaseAuditLog
from app.models.case_details import CaseDesign, CaseForeignInfo, CaseLitigation, CasePatent, CaseTrademark
from app.models.case_flat_index import CaseFlatIndex
from app.models.case_group import CaseGroup
from app.models.client import Client
from app.models.codes import Code, CodeGroup
from app.models.communication import (
    Communication,
    CommunicationFileAsset,
    OfficeAction,
    OfficeActionFileAsset,
)
from app.models.crm import CRMActivity, CRMContact, CRMLead, CRMOpportunity
from app.models.crm_client_merge_log import CRMClientMergeLog
from app.models.deadline import Deadline, RenewalFee
from app.models.deletion_log import DeletionLog
from app.models.document import Acl, Document, DocumentVersion, Folder
from app.models.error_report import ErrorReport
from app.models.file_delete_queue import FileDeleteQueue
from app.models.job_run import JobRun
from app.models.letter import Letter
from app.models.matter import MatterEvent
from app.models.matter_facts import MatterFacts
from app.models.notification import NotificationLog
from app.models.notification_queue import NotificationQueue
from app.models.operation import Operation, OperationChange
from app.ops.models import DiskSample, DurableJob
from app.models.parse_failure import ParseFailure
from app.models.party import Party, PartyAddress, PartyCode, PartyContact, PartyStaff
from app.models.ip_records import (
    AnnuityItem,
    AutomationChangeSet,
    AutomationChangeSnapshot,
    AutomationFieldFeedback,
    AutomationReviewFeedback,
    BillingGuardrailFinding,
    CaseExpenseInvoiceMap,
    CitedReference,
    DeadlineReviewQueue,
    DocumentSearchIndex,
    DocketItem,
    EmailAttachment,
    EmailIngestionLog,
    EmailMessage,
    EmailMessageMatterLink,
    EmailMessageTombstone,
    EventKeyMap,
    ExternalInvoiceCaseLink,
    ExternalInvoiceCaseMap,
    ExtractionResult,
    Family,
    FieldEvidence,
    IngestionRun,
    MailMatchCandidate,
    Matter,
    MatterCustomField,
    MatterFamily,
    MatterRiskFact,
    MatterStatusRecalcQueue,
    MatterIdentifier,
    MatterMatch,
    MatterMemo,
    MatterMemoFileAsset,
    MatterPartyRole,
    MatterProgress,
    MatterStaffAssignment,
    MatterStatusHistory,
    LegacyExpense,
    LegacyExpensePayment,
    LegacyInvoice,
    LegacyInvoicePayment,
    RawImportField,
    WorkflowPlaybookTemplate,
)
from app.models.role import Role, user_roles
from app.models.system_config import SystemConfig
from app.models.ui_prefs import AutomationReviewTemplate, UserUiPreference
from app.models.undo_action import UndoAction
from app.models.user import User
from app.models.user_access_log import UserAccessLog
from app.models.user_saved_view import UserSavedView
from app.models.workflow import Workflow
from app.models.workflow_assignment_request import WorkflowAssignmentRequest
from app.models.workflow_checklist import WorkflowChecklistItem, WorkflowReminderSent
from app.models.worklog import WorkLog
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


def _normalize_pg_column_type(col_type: str) -> str:
    """
    Normalize SQLite-ish column type strings to PostgreSQL-friendly DDL fragments.

    This is used by db_startup.add_column() which may run against PostgreSQL.
    """
    raw = (col_type or "").strip()
    if not raw:
        return raw

    upper = raw.upper()
    if upper.startswith("REAL"):
        return re.sub(r"(?i)^REAL\b", "DOUBLE PRECISION", raw, count=1)
    if upper.startswith("DATETIME"):
        return re.sub(r"(?i)^DATETIME\b", "TIMESTAMP", raw, count=1)
    if upper.startswith("BOOLEAN"):
        out = re.sub(r"(?i)^BOOLEAN\b", "BOOLEAN", raw, count=1)
        out = re.sub(r"(?i)\bDEFAULT\s+1\b", "DEFAULT TRUE", out)
        out = re.sub(r"(?i)\bDEFAULT\s+0\b", "DEFAULT FALSE", out)
        return out
    if upper.startswith("INTEGER"):
        return re.sub(r"(?i)^INTEGER\b", "INTEGER", raw, count=1)
    if upper.startswith("TEXT"):
        return re.sub(r"(?i)^TEXT\b", "TEXT", raw, count=1)
    if upper.startswith("DATE"):
        return re.sub(r"(?i)^DATE\b", "DATE", raw, count=1)

    return raw


# Regex patterns for allowed PostgreSQL DDL fragments.
# Only these patterns may appear in the type string passed to ALTER TABLE ... ADD COLUMN.
_SAFE_PG_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_PG_TYPE_RE = re.compile(
    r"^(?:"
    r"TEXT|INTEGER|BOOLEAN|DATE|TIMESTAMP|REAL|DOUBLE PRECISION|FLOAT|SERIAL|BIGINT|SMALLINT|NUMERIC"
    r"|VARCHAR\s*\(\s*\d+\s*\)"
    r"|NUMERIC\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\)"
    r")"
    r"(?:\s+DEFAULT\s+(?:'[^']*'|\d+(?:\.\d+)?|TRUE|FALSE|NULL|CURRENT_TIMESTAMP))?"
    r"(?:\s+NOT\s+NULL)?"
    r"$",
    re.IGNORECASE,
)


def _quote_pg_identifier(identifier: str) -> str:
    """Return a double-quoted PostgreSQL identifier after whitelist validation."""
    normalized = (identifier or "").strip()
    if not _SAFE_PG_IDENTIFIER_RE.match(normalized):
        raise ValueError(f"unsafe PostgreSQL identifier: {identifier!r}")
    return f'"{normalized}"'


def _is_safe_pg_type(pg_type: str) -> bool:
    """Whitelist validation for PostgreSQL column type DDL fragments.

    Prevents SQL injection via the type parameter in ALTER TABLE ... ADD COLUMN statements.
    Only well-known PostgreSQL type patterns with optional DEFAULT/NOT NULL clauses are allowed.
    """
    normalized = (pg_type or "").strip()
    if not normalized:
        return False
    return bool(_SAFE_PG_TYPE_RE.match(normalized))


def create_tables(app):
    """
    Ensure all tables and columns exist.
    Refactored from app/__init__.py to improve readability.
    """
    base_dir = os.path.abspath(os.path.join(app.root_path, os.pardir))
    ddl_failures = []

    def _record_ddl_failure(action: str, exc=None):
        msg = action if exc is None else f"{action}: {exc}"
        ddl_failures.append(msg)
        try:
            app.logger.warning(f"DB startup DDL failed: {msg}")
        except Exception as log_exc:
            report_swallowed_exception(
                log_exc,
                context="db_startup.create_tables._record_ddl_failure.logger_warning",
                log_key="db_startup.create_tables._record_ddl_failure.logger_warning",
                log_window_seconds=300,
            )

    def add_column(table: str, col_name: str, col_type: str):
        """Add column if it doesn't exist (PostgreSQL)."""
        try:
            table_sql = _quote_pg_identifier(table)
            col_sql = _quote_pg_identifier(col_name)
        except ValueError as exc:
            _record_ddl_failure(
                f"unsafe identifier rejected for add_column {table}.{col_name}", exc
            )
            return
        insp_local = inspect(db.engine)
        if not insp_local.has_table(table):
            return
        cols = [c["name"] for c in insp_local.get_columns(table)]
        if col_name not in cols:
            pg_type = _normalize_pg_column_type(col_type)
            if not _is_safe_pg_type(pg_type):
                _record_ddl_failure(f"unsafe pg_type rejected: {pg_type!r} for {table}.{col_name}")
                return
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(f"ALTER TABLE {table_sql} ADD COLUMN {col_sql} {pg_type}"))
                    conn.commit()
            except Exception as exc:
                # Column may already exist or other error
                _record_ddl_failure(f"add_column {table}.{col_name} ({pg_type})", exc)

    def ensure_text_column(table: str, col_name: str):
        """Ensure an existing PostgreSQL column is TEXT."""
        if db.engine.dialect.name != "postgresql":
            return
        try:
            table_sql = _quote_pg_identifier(table)
            col_sql = _quote_pg_identifier(col_name)
        except ValueError as exc:
            _record_ddl_failure(
                f"unsafe identifier rejected for ensure_text_column {table}.{col_name}",
                exc,
            )
            return

        insp_local = inspect(db.engine)
        if not insp_local.has_table(table):
            return
        column = next(
            (c for c in insp_local.get_columns(table) if c.get("name") == col_name),
            None,
        )
        if column is None:
            return
        type_name = str(column.get("type") or "").upper()
        if any(token in type_name for token in ("TEXT", "CHAR", "VARCHAR", "STRING")):
            return
        try:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        f"ALTER TABLE {table_sql} ALTER COLUMN {col_sql} "
                        f"TYPE TEXT USING {col_sql}::text"
                    )
                )
                conn.commit()
        except Exception as exc:
            _record_ddl_failure(f"ensure_text_column {table}.{col_name}", exc)

    def ensure_matter_overview_view():
        """Create the matter overview view used by list/detail workflows in dev bootstrap."""
        if db.engine.dialect.name != "postgresql":
            return
        insp_local = inspect(db.engine)
        invoice_columns = set()
        if insp_local.has_table("billing_invoices"):
            invoice_columns = {
                c["name"] for c in insp_local.get_columns("billing_invoices")
            }
        if {"ipm_case_id", "total", "payment_status", "is_deleted"} <= invoice_columns:
            invoice_agg_sql = """
                            SELECT
                                bi.ipm_case_id AS matter_id,
                                SUM(COALESCE(bi.total, 0))::double precision AS billed_total,
                                SUM(
                                    CASE
                                        WHEN lower(COALESCE(bi.payment_status, '')) IN (
                                            'paid', 'complete', 'completed', 'settled'
                                        )
                                        THEN COALESCE(bi.total, 0)
                                        ELSE 0
                                    END
                                )::double precision AS received_total
                            FROM public.billing_invoices bi
                            WHERE COALESCE(bi.is_deleted, 0) = 0
                              AND NULLIF(trim(COALESCE(bi.ipm_case_id, '')), '') IS NOT NULL
                            GROUP BY bi.ipm_case_id
            """
        else:
            invoice_agg_sql = """
                            SELECT
                                NULL::text AS matter_id,
                                0::double precision AS billed_total,
                                0::double precision AS received_total
                            WHERE false
            """
        try:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        DO $$
                        DECLARE k char;
                        BEGIN
                            SELECT c.relkind INTO k
                            FROM pg_class c
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'public' AND c.relname = 'v_matter_overview';

                            IF k = 'v' THEN
                                EXECUTE 'DROP VIEW public.v_matter_overview';
                            ELSIF k = 'm' THEN
                                EXECUTE 'DROP MATERIALIZED VIEW public.v_matter_overview';
                            ELSIF k IS NOT NULL THEN
                                EXECUTE 'DROP TABLE public.v_matter_overview';
                            END IF;
                        END $$;
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE OR REPLACE VIEW public.v_matter_overview AS
                        WITH party_agg AS (
                            SELECT
                                r.matter_id,
                                string_agg(COALESCE(p.name_display, r.raw_text), '; ' ORDER BY r.seq)
                                    FILTER (WHERE lower(r.role_code) = 'client') AS clients,
                                string_agg(COALESCE(p.name_display, r.raw_text), '; ' ORDER BY r.seq)
                                    FILTER (WHERE lower(r.role_code) = 'applicant') AS applicants,
                                string_agg(COALESCE(p.name_display, r.raw_text), '; ' ORDER BY r.seq)
                                    FILTER (WHERE lower(r.role_code) = 'attorney') AS attorneys
                            FROM public.matter_party_role r
                            LEFT JOIN public.party p ON p.party_id = r.party_id
                            GROUP BY r.matter_id
                        ),
                        family_agg AS (
                            SELECT
                                mf.matter_id,
                                string_agg(f.family_key, '; ' ORDER BY f.family_key) AS family_keys,
                                max(COALESCE(mf.is_lead, 0))::integer AS is_family_lead
                            FROM public.matter_family mf
                            JOIN public.family f ON f.family_id = mf.family_id
                            GROUP BY mf.matter_id
                        ),
                        docket_agg AS (
                            SELECT
                                d.matter_id,
                                count(*) FILTER (
                                    WHERE d.done_date IS NULL
                                      AND COALESCE(d.is_deleted, false) = false
                                )::integer AS open_docket_count
                            FROM public.docket_item d
                            GROUP BY d.matter_id
                        ),
                        next_docket AS (
                            SELECT DISTINCT ON (d.matter_id)
                                d.matter_id,
                                COALESCE(
                                    NULLIF(trim(d.extended_due_date), ''),
                                    NULLIF(trim(d.due_date), '')
                                ) AS next_due_date,
                                COALESCE(
                                    NULLIF(trim(d.name_ref), ''),
                                    NULLIF(trim(d.name_free), '')
                                ) AS next_due_name
                            FROM public.docket_item d
                            WHERE d.done_date IS NULL
                              AND COALESCE(d.is_deleted, false) = false
                            ORDER BY
                                d.matter_id,
                                COALESCE(
                                    NULLIF(trim(d.extended_due_date), ''),
                                    NULLIF(trim(d.due_date), '')
                                ) NULLS LAST,
                                d.docket_id
                        ),
                        invoice_agg AS (
{invoice_agg_sql}
                        )
                        SELECT
                            m.matter_id,
                            m.our_ref,
                            m.old_our_ref,
                            m.your_ref,
                            m.right_name,
                            m.right_group,
                            m.matter_type,
                            m.status_red,
                            m.status_blue,
                            m.inhouse_status,
                            m.retained_at,
                            m.entered_at,
                            m.created_at,
                            pa.clients,
                            pa.applicants,
                            pa.attorneys,
                            fa.family_keys,
                            COALESCE(fa.is_family_lead, 0)::integer AS is_family_lead,
                            nd.next_due_date,
                            nd.next_due_name,
                            COALESCE(da.open_docket_count, 0)::integer AS open_docket_count,
                            COALESCE(ia.billed_total, 0)::double precision AS billed_total,
                            COALESCE(ia.received_total, 0)::double precision AS received_total,
                            (
                                COALESCE(ia.billed_total, 0) - COALESCE(ia.received_total, 0)
                            )::double precision AS outstanding_total,
                            0::double precision AS exp_requested_total,
                            0::double precision AS exp_remit_total,
                            0::double precision AS exp_outstanding_total
                        FROM public.matter m
                        LEFT JOIN party_agg pa ON pa.matter_id = m.matter_id
                        LEFT JOIN family_agg fa ON fa.matter_id = m.matter_id
                        LEFT JOIN docket_agg da ON da.matter_id = m.matter_id
                        LEFT JOIN next_docket nd ON nd.matter_id = m.matter_id
                        LEFT JOIN invoice_agg ia ON ia.matter_id = m.matter_id
                        WHERE COALESCE(m.is_deleted, false) = false
                        """
                    )
                )
                conn.commit()
        except Exception as exc:
            _record_ddl_failure("ensure view v_matter_overview", exc)

    insp = inspect(db.engine)

    for m in (
        Client,
        Role,
        User,
        UserAccessLog,
        UserSavedView,
        UserUiPreference,
        AutomationReviewTemplate,
        ErrorReport,
        AuditLog,
        DeletionLog,
        Operation,
        OperationChange,
        BackupSet,
        JobRun,
        DurableJob,
        DiskSample,
        SystemConfig,
        Party,
        PartyStaff,
        PartyCode,
        CodeGroup,
        Code,
        PartyContact,
        PartyAddress,
        Matter,
        MatterStatusRecalcQueue,
        MatterIdentifier,
        MatterPartyRole,
        MatterStaffAssignment,
        MatterProgress,
        MatterStatusHistory,
        Family,
        MatterFamily,
        EventKeyMap,
        Workflow,
        WorkflowAssignmentRequest,
        DocketItem,
        AnnuityItem,
        MatterCustomField,
        MatterMemo,
        MatterMemoFileAsset,
        ExternalInvoiceCaseLink,
        ExternalInvoiceCaseMap,
        LegacyInvoice,
        LegacyInvoicePayment,
        LegacyExpense,
        LegacyExpensePayment,
        CaseExpenseInvoiceMap,
        RawImportField,
        Case,
        Deadline,
        RenewalFee,
        Letter,
        Folder,
        Document,
        DocumentVersion,
        Acl,
        CaseGroup,
        WorkLog,
        CaseAuditLog,
        CaseFlatIndex,
        MatterEvent,
        FileAsset,
        MatterFileAsset,
        FileDeleteQueue,
        Communication,
        CommunicationFileAsset,
        OfficeAction,
        OfficeActionFileAsset,
        CRMLead,
        CRMOpportunity,
        CRMContact,
        CRMActivity,
        CRMClientMergeLog,
        AnnuityWorkflowSyncQueue,
        AnnuityWorkflowSyncDeadLetter,
        MatterFacts,
        MatterRiskFact,
        DeadlineReviewQueue,
        ParseFailure,
        NotificationLog,
        NotificationQueue,
        EmailMessage,
        EmailMessageMatterLink,
        EmailMessageTombstone,
        EmailAttachment,
        IngestionRun,
        ExtractionResult,
        FieldEvidence,
        MatterMatch,
        MailMatchCandidate,
        AutomationChangeSet,
        AutomationChangeSnapshot,
        AutomationReviewFeedback,
        AutomationFieldFeedback,
        EmailIngestionLog,
        DocumentSearchIndex,
        BillingGuardrailFinding,
        WorkflowPlaybookTemplate,
        WorkflowChecklistItem,
        WorkflowReminderSent,
        UndoAction,
        CasePatent,
        CaseDesign,
        CaseTrademark,
        CaseLitigation,
        CaseForeignInfo,
    ):
        if not insp.has_table(m.__tablename__):
            m.__table__.create(db.engine, checkfirst=True)
    user_roles.create(db.engine, checkfirst=True)

    if inspect(db.engine).has_table("notification_queue"):
        add_column("notification_queue", "docket_id", "TEXT")
        ensure_text_column("notification_queue", "docket_id")

    # Ensure case_flat_index columns
    if insp.has_table("case_flat_index"):
        add_column("case_flat_index", "inventor", "TEXT")
        add_column("case_flat_index", "client_name", "TEXT")

    # matter_event.raw_text: stores non-date extracted values (e.g., exam_request, claim_count).
    if insp.has_table("matter_event"):
        add_column("matter_event", "raw_text", "TEXT")

    # matter_* role ordering compatibility:
    # some legacy DBs were missing these columns when migrations were skipped.
    if insp.has_table("matter_party_role"):
        add_column("matter_party_role", "seq", "INTEGER")
        add_column("matter_party_role", "raw_text", "TEXT")
    if insp.has_table("matter_staff_assignment"):
        add_column("matter_staff_assignment", "seq", "INTEGER")
        add_column("matter_staff_assignment", "raw_text", "TEXT")

    # external_invoice_case_link.ipm_invoice_id: backwards-compat with legacy ipm_invoice_id.
    if insp.has_table("external_invoice_case_link"):
        cols_ext_link = [c["name"] for c in insp.get_columns("external_invoice_case_link")]
        if "ipm_invoice_id" not in cols_ext_link:
            add_column("external_invoice_case_link", "ipm_invoice_id", "TEXT")
            if "ipm_invoice_id" in cols_ext_link:
                try:
                    with db.engine.connect() as conn:
                        conn.execute(
                            text(
                                "UPDATE external_invoice_case_link "
                                "SET ipm_invoice_id = ipm_invoice_id "
                                "WHERE ipm_invoice_id IS NULL"
                            )
                        )
                        conn.commit()
                except Exception as exc:
                    _record_ddl_failure(
                        "backfill external_invoice_case_link.ipm_invoice_id from ipm_invoice_id",
                        exc,
                    )

    insp = inspect(db.engine)

    # cases.case_type
    if insp.has_table("cases"):
        cols_cases = [c["name"] for c in insp.get_columns("cases")]
        if "case_type" not in cols_cases:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE cases ADD COLUMN case_type VARCHAR(20)"))
                conn.commit()

    # users.is_active
    if insp.has_table("users"):
        cols_users = [c["name"] for c in insp.get_columns("users")]
        if "display_name" not in cols_users:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN display_name VARCHAR(120)"))
                conn.commit()
        if "staff_party_id" not in cols_users:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN staff_party_id TEXT"))
                conn.commit()
        if "is_active" not in cols_users:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                conn.commit()
        if "menu_favorites" not in cols_users:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN menu_favorites TEXT DEFAULT '[]'"))
                conn.commit()
        # Backfill staff_party_id for existing users (best-effort).
        try:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE users
                        SET staff_party_id = (
                            SELECT ps.party_id
                            FROM party_staff ps
                            WHERE lower(COALESCE(ps.staff_code, '')) = lower(COALESCE(users.username, ''))
                            LIMIT 1
                        )
                        WHERE staff_party_id IS NULL
                            AND COALESCE(username, '') != ''
                        """
                    )
                )
                conn.commit()
        except Exception as exc:
            # Best-effort: backfill should not block startup.
            report_swallowed_exception(
                exc,
                context="db_startup.create_tables.backfill_staff_party_id",
                log_key="db_startup.create_tables.backfill_staff_party_id",
                log_window_seconds=300,
            )

    if insp.has_table("deletion_logs"):
        add_column("deletion_logs", "entity_key", "TEXT")
        add_column("deletion_logs", "restored_entity_id", "INTEGER")
        add_column("deletion_logs", "restored_entity_key", "TEXT")
        add_column("deletion_logs", "restored_by", "INTEGER")
        add_column("deletion_logs", "restored_at", "DATETIME")
        add_column("deletion_logs", "parent_type", "VARCHAR(50)")
        add_column("deletion_logs", "parent_id", "TEXT")
        add_column("deletion_logs", "search_vector", "TEXT")
        add_column("deletion_logs", "tags", "VARCHAR(255)")

    if insp.has_table("error_reports"):
        add_column("error_reports", "request_id", "TEXT")
        add_column("error_reports", "matter_id", "TEXT")
        add_column("error_reports", "invoice_id", "TEXT")
        add_column("error_reports", "workflow_id", "TEXT")

    # Case detail tables (joined inheritance)
    add_column("case_patents", "app_route", "VARCHAR(30)")
    add_column("case_patents", "reg_date", "DATE")
    add_column("case_patents", "reg_no", "VARCHAR(50)")
    add_column("case_patents", "original_app_date", "DATE")
    add_column("case_patents", "original_app_no", "VARCHAR(50)")
    add_column("case_patents", "claims_dep", "INTEGER")

    add_column("case_designs", "drawing_count", "INTEGER")
    add_column("case_designs", "app_route", "VARCHAR(30)")
    add_column("case_designs", "original_app_date", "DATE")
    add_column("case_designs", "original_app_no", "VARCHAR(50)")
    add_column("case_designs", "pub_date", "DATE")
    add_column("case_designs", "pub_no", "VARCHAR(50)")
    add_column("case_designs", "reg_date", "DATE")
    add_column("case_designs", "reg_no", "VARCHAR(50)")

    add_column("case_trademarks", "app_route", "VARCHAR(30)")
    add_column("case_trademarks", "original_app_date", "DATE")
    add_column("case_trademarks", "original_app_no", "VARCHAR(50)")
    add_column("case_trademarks", "original_reg_date", "DATE")
    add_column("case_trademarks", "original_reg_no", "VARCHAR(50)")
    add_column("case_trademarks", "nice_classes", "VARCHAR(100)")
    add_column("case_trademarks", "designated_goods", "TEXT")
    add_column("case_trademarks", "goods_info", "TEXT")

    # ipm costs: optional extensions
    # NOTE: moved to Alembic migration 20260110_case_expense_migration
    # add_column("invoice", "tax_no", "TEXT")
    # add_column("invoice", "description", "TEXT")
    # add_column("expense", "description", "TEXT")
    # add_column("expense", "vendor_name", "TEXT")
    # add_column("expense", "category_code", "TEXT")
    # add_column("expense", "expense_date", "TEXT")
    # add_column("expense", "due_date", "TEXT")
    # try:
    #     with db.engine.connect() as conn:
    #         conn.execute(
    #             text(
    #                 "CREATE UNIQUE INDEX IF NOT EXISTS uq_case_expense_invoice_map "
    #                 "ON case_expense_invoice_map(expense_id, billing_invoice_id, billing_line_item_id)"
    #             )
    #         )
    #         conn.execute(
    #             text(
    #                 "CREATE INDEX IF NOT EXISTS idx_ceim_matter "
    #                 "ON case_expense_invoice_map(matter_id)"
    #             )
    #         )
    #         conn.execute(
    #             text(
    #                 "CREATE INDEX IF NOT EXISTS idx_ceim_expense "
    #                 "ON case_expense_invoice_map(expense_id)"
    #             )
    #         )
    #         conn.execute(
    #             text(
    #                 "CREATE INDEX IF NOT EXISTS idx_ceim_invoice "
    #                 "ON case_expense_invoice_map(billing_invoice_id)"
    #             )
    #         )
    #         conn.commit()
    # except Exception as exc:
    #     _record_ddl_failure("create indices for case_expense_invoice_map", exc)
    add_column("clients", "external_invoice_client_id", "INTEGER")
    add_column("clients", "manager", "TEXT")
    add_column("clients", "notes", "TEXT")
    add_column("clients", "search_tags", "TEXT")
    add_column("clients", "biz_reg_number", "TEXT")
    add_column("clients", "biz_company_name", "TEXT")
    add_column("clients", "biz_representative_name", "TEXT")
    add_column("clients", "biz_opening_date", "TEXT")
    add_column("clients", "biz_corp_registration_number", "TEXT")
    add_column("clients", "biz_business_location", "TEXT")
    add_column("clients", "biz_head_office_location", "TEXT")
    add_column("clients", "biz_business_type", "TEXT")
    add_column("clients", "biz_tax_invoice_email", "TEXT")
    add_column("clients", "ipm_party_id", "TEXT")
    add_column("clients", "ipm_client_id", "INTEGER")
    add_column("invoice", "external_invoice_id", "INTEGER")
    add_column("invoice", "external_invoice_number", "TEXT")
    add_column("invoice", "external_invoice_url", "TEXT")

    add_column("matter_file_asset", "doc_type", "TEXT")
    add_column("matter_file_asset", "tags", "TEXT")
    add_column("matter_file_asset", "previewable", "BOOLEAN DEFAULT 0")

    try:
        with db.engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_annuity_workflow_sync_queue_next_run_at "
                    "ON annuity_workflow_sync_queue(next_run_at)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_annuity_workflow_sync_queue_locked_at "
                    "ON annuity_workflow_sync_queue(locked_at)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_matter_facts_reg_right "
                    "ON matter_facts(registration_date, right_type_norm)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_annuity_workflow_sync_dead_letter_matter_id "
                    "ON annuity_workflow_sync_dead_letter(matter_id)"
                )
            )
            conn.commit()
    except Exception as exc:
        _record_ddl_failure("create indices for annuity queue/facts", exc)


    try:
        with db.engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_parse_failure_created_at "
                    "ON parse_failure(created_at)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_parse_failure_kind_source "
                    "ON parse_failure(kind, source)"
                )
            )
            conn.commit()
    except Exception as exc:
        _record_ddl_failure("create indices for parse_failure", exc)

    try:
        if getattr(db.engine.dialect, "name", "") == "postgresql":
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        CREATE INDEX IF NOT EXISTS ix_annuity_item_effective_due_text
                        ON annuity_item (
                            substr(
                                COALESCE(
                                    NULLIF(trim(internal_due_date), ''),
                                    NULLIF(trim(extended_due_date), ''),
                                    NULLIF(trim(due_date), '')
                                ),
                                1,
                                10
                            )
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE INDEX IF NOT EXISTS ix_docket_item_effective_due_text
                        ON docket_item (
                            substr(
                                COALESCE(
                                    NULLIF(trim(extended_due_date), ''),
                                    NULLIF(trim(due_date), '')
                                ),
                                1,
                                10
                            )
                        )
                        """
                    )
                )
                conn.commit()
    except Exception as exc:
        _record_ddl_failure("create functional indices for due dates", exc)
    # ipm communication: "()" ( Entry date)
    add_column("communication", "received_date", "TEXT")
    add_column("communication", "search_compact", "TEXT")
    add_column("office_action", "search_compact", "TEXT")

    # ipm annuity: owner assignment for renewal filtering/workflow sync
    add_column("annuity_item", "owner_staff_party_id", "TEXT")

    # --- New: annuity workflow durable queue / dead-letter ---
    add_column("annuity_workflow_sync_queue", "payload", "TEXT")
    add_column("annuity_workflow_sync_queue", "attempts", "INTEGER")
    add_column("annuity_workflow_sync_queue", "next_run_at", "DATETIME")
    add_column("annuity_workflow_sync_queue", "locked_at", "DATETIME")
    add_column("annuity_workflow_sync_queue", "lock_token", "TEXT")
    add_column("annuity_workflow_sync_queue", "last_error", "TEXT")
    add_column("annuity_workflow_sync_queue", "created_at", "DATETIME")
    add_column("annuity_workflow_sync_queue", "updated_at", "DATETIME")

    add_column("matter_facts", "registration_date", "DATE")
    add_column("matter_facts", "registration_date_source", "TEXT")
    add_column("matter_facts", "right_type_norm", "TEXT")
    add_column("matter_facts", "updated_at", "DATETIME")

    # --- Uniqueness safety (prevents duplicate task rows) ---
    # Align with model-level unique index (matter_id, name_ref, due_date) on open rows.
    # NOTE: If legacy duplicates exist, these DDLs can fail safely (logged) and startup continues.
    try:
        with db.engine.connect() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ux_docket_matter_name_ref"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_docket_item_open_natural "
                    "ON docket_item(matter_id, name_ref, due_date) "
                    "WHERE done_date IS NULL AND is_deleted = false AND name_ref IS NOT NULL"
                )
            )
            conn.commit()
    except Exception as exc:
        _record_ddl_failure("create index ux_docket_item_open_natural", exc)

    try:
        insp = inspect(db.engine)
        if insp.has_table("annuity_item"):
            cols = {c["name"]: c for c in insp.get_columns("annuity_item")}
            cycle_col = cols.get("cycle_no")
            if cycle_col and cycle_col.get("nullable", True):
                with db.engine.connect() as conn:
                    has_null = conn.execute(
                        text("SELECT 1 FROM annuity_item WHERE cycle_no IS NULL LIMIT 1")
                    ).first()
                    if not has_null:
                        conn.execute(
                            text("ALTER TABLE annuity_item ALTER COLUMN cycle_no SET NOT NULL")
                        )
                        conn.commit()
    except Exception as exc:
        _record_ddl_failure("set annuity_item.cycle_no NOT NULL", exc)

    try:
        insp = inspect(db.engine)
        if insp.has_table("annuity_item"):
            checks = {c.get("name") for c in insp.get_check_constraints("annuity_item")}
            if "ck_annuity_cycle_no_positive" not in checks:
                with db.engine.connect() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE annuity_item "
                            "ADD CONSTRAINT ck_annuity_cycle_no_positive CHECK (cycle_no > 0)"
                        )
                    )
                    conn.commit()
    except Exception as exc:
        _record_ddl_failure("create check constraint ck_annuity_cycle_no_positive", exc)

    # NOTE: Legacy data backfill operations (email uploads, communication dates)
    # have been removed during SQLite to PostgreSQL conversion.
    # Use explicit maintenance jobs for future data backfills.

    # ipm matter: right_name should track Matter "Proposed title" (Matter).
    # Backfill when right_name is blank or still a generic  label.
    try:
        insp = inspect(db.engine)
        if insp.has_table("matter") and insp.has_table("raw_import_field"):
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE matter
                            SET right_name = (
                                SELECT f.value_text
                                    FROM raw_import_field f
                                WHERE f.raw_id = matter.raw_id
                                    AND f.sheet_name = 'Matter'
                                    AND f.source_column = 'Proposed title'
                                    AND f.value_text IS NOT NULL
                                    AND TRIM(f.value_text) <> ''
                            )
                            WHERE raw_id IS NOT NULL
                            AND (
                                right_name IS NULL OR TRIM(right_name) = ''
                                OR right_name IN (
                                'DomesticPatent','DomesticTrademark','DomesticDesign',
                                'Incoming Patent','Incoming Trademark','Incoming Design',
                                'Outgoing Patent','Outgoing Trademark','Outgoing Design',
                                '//Litigation','Other'
                                )
                            )
                            AND EXISTS (
                                SELECT 1
                                    FROM raw_import_field f2
                                WHERE f2.raw_id = matter.raw_id
                                    AND f2.sheet_name = 'Matter'
                                    AND f2.source_column = 'Proposed title'
                                    AND f2.value_text IS NOT NULL
                                    AND TRIM(f2.value_text) <> ''
                            )
                        """
                    )
                )
                conn.commit()
    except Exception as exc:
        # Best-effort data fixup should not block startup.
        report_swallowed_exception(
            exc,
            context="db_startup.create_tables.backfill_matter_right_name",
            log_key="db_startup.create_tables.backfill_matter_right_name",
            log_window_seconds=300,
        )

    # Link app.clients -> ipm.party (optional)
    add_column("clients", "party_id", "TEXT")

    # workflows: extend fields for case "MatterResponsibleTask"
    add_column("workflows", "business_code", "TEXT")
    add_column("workflows", "priority", "TEXT")
    add_column("workflows", "request_start_date", "DATE")
    add_column("workflows", "legal_due_date", "DATE")
    add_column("workflows", "source_docket_due_date", "DATE")
    add_column("workflows", "source_docket_legal_due_date", "DATE")
    add_column("workflows", "draft_due_date", "DATE")
    add_column("workflows", "draft_due_date2", "DATE")
    add_column("workflows", "submit_due_date", "DATE")
    add_column("workflows", "draft_sent_date", "DATE")
    add_column("workflows", "submit_date", "DATE")
    add_column("workflows", "difficulty", "REAL")
    add_column("workflows", "page_count", "INTEGER")
    add_column("workflows", "work_hours", "REAL")
    add_column("workflows", "inspector_id", "INTEGER")
    add_column("workflows", "attorney_assignee_id", "INTEGER")
    add_column("workflows", "send_memo", "TEXT")
    try:
        with db.engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_workflows_business_code "
                    "ON workflows(business_code) "
                    "WHERE business_code IS NOT NULL"
                )
            )
            conn.commit()
    except Exception as exc:
        _record_ddl_failure("create index ux_workflows_business_code", exc)

    # Materialize raw_json_only fields (one-time, best-effort).
    # This migrates data out of raw_import_row.row_json into raw_import_field for DB-first querying.
    try:
        insp = inspect(db.engine)
        if insp.has_table("raw_import_row"):
            add_column("raw_import_row", "payload_hash", "TEXT")
            try:
                with db.engine.connect() as conn:
                    conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS raw_import_payload (
                                payload_hash TEXT PRIMARY KEY,
                                payload_json TEXT NOT NULL,
                                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                            )
                            """
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_raw_import_row_payload_hash "
                            "ON raw_import_row(payload_hash);"
                        )
                    )
                    conn.commit()
            except Exception as exc:
                _record_ddl_failure("create table raw_import_payload", exc)
            has_raw_import_field = insp.has_table("raw_import_field")
            if not has_raw_import_field:
                with db.engine.connect() as conn:
                    conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS raw_import_field (
                                raw_field_id TEXT PRIMARY KEY,
                                raw_id TEXT NOT NULL,
                                sheet_name TEXT NOT NULL,
                                source_column TEXT NOT NULL,
                                value_text TEXT,
                                created_at TEXT,
                                UNIQUE(raw_id, source_column)
                            );
                            """
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_raw_import_field_raw_id "
                            "ON raw_import_field(raw_id);"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_raw_import_field_sheet "
                            "ON raw_import_field(sheet_name, source_column);"
                        )
                    )
                    conn.commit()
            has_any = False
            if has_raw_import_field:
                with db.engine.connect() as conn:
                    has_any = bool(
                        conn.execute(text("SELECT 1 FROM raw_import_field LIMIT 1")).fetchone()
                    )
    except Exception as exc:
        _record_ddl_failure("raw_import materialization", exc)

    ensure_matter_overview_view()

    if ddl_failures:
        msg = f"DB startup DDL failures ({len(ddl_failures)}): " + "; ".join(ddl_failures)
        if app.config.get("DB_SCHEMA_FAIL_FAST"):
            raise RuntimeError(msg)
        try:
            app.logger.warning(msg)
        except Exception as log_exc:
            report_swallowed_exception(
                log_exc,
                context="db_startup.create_tables.ddl_failures_logger_warning",
                log_key="db_startup.create_tables.ddl_failures_logger_warning",
                log_window_seconds=300,
            )

def _check_required_system_config(app) -> tuple[dict, bool]:
    required = app.config.get("STARTUP_REQUIRED_SYSTEM_CONFIG_KEYS") or []
    missing: list[str] = []
    if not required:
        return {"ok": True, "missing": []}, True
    try:
        for key in required:
            if not key:
                continue
            row = SystemConfig.query.filter_by(key=key).first()
            value = (row.value if row else "") if row else ""
            if not str(value or "").strip():
                missing.append(key)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="db_startup.system_config_check",
            log_key="db_startup.system_config_check",
            log_window_seconds=300,
        )
        return {"ok": False, "missing": required, "error": type(exc).__name__}, False
    return {"ok": not missing, "missing": missing}, not missing


def _check_paths(app) -> tuple[dict, bool]:
    upload_dir = (app.config.get("UPLOAD_FOLDER") or "").strip()
    mail_sync_lock_path = (
        os.environ.get("MAIL_SYNC_CHECKPOINT_LOCK_PATH") or "data/.locks/mail_sync_checkpoint.lock"
    ).strip()
    paths = {
        "upload_dir": upload_dir,
        "email_upload_dir": os.path.join(upload_dir, "emails") if upload_dir else "",
        "attachments_dir": (app.config.get("ATTACHMENTS_DIR") or "").strip(),
        "client_attachments_dir": (app.config.get("CLIENT_ATTACHMENTS_DIR") or "").strip(),
        "pdf_text_cache_dir": (app.config.get("PDF_TEXT_CACHE_DIR") or "").strip(),
        "backup_dir": (app.config.get("BACKUP_DIR") or "").strip(),
        "mail_sync_checkpoint_lock_dir": (
            os.path.dirname(mail_sync_lock_path) if mail_sync_lock_path else ""
        ),
    }
    results: dict[str, str] = {}
    ok = True
    for label, path in paths.items():
        if not path:
            results[label] = "missing"
            ok = False
            continue
        try:
            os.makedirs(path, exist_ok=True)
            if os.access(path, os.W_OK):
                results[label] = "ok"
            else:
                results[label] = "not_writable"
                ok = False
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context=f"db_startup.path_check.{label}",
                log_key=f"db_startup.path_check.{label}",
                log_window_seconds=300,
            )
            results[label] = f"error:{type(exc).__name__}"
            ok = False
    return results, ok


def _check_integrations(app) -> tuple[dict, bool]:
    return {"external_integrations": "removed"}, True


def collect_startup_checks(app) -> tuple[bool, dict]:
    if not app.config.get("STARTUP_CHECKS_ENABLED", True):
        return True, {"enabled": False}

    checks: dict[str, object] = {}
    ok = True

    system_config_checks, system_ok = _check_required_system_config(app)
    checks["system_config"] = system_config_checks
    if not system_ok:
        ok = False

    path_checks, path_ok = _check_paths(app)
    checks["paths"] = path_checks
    if not path_ok:
        ok = False

    integration_checks, integration_ok = _check_integrations(app)
    checks["integrations"] = integration_checks
    if not integration_ok:
        ok = False

    return ok, checks


def run_startup_checks(app) -> None:
    if not app.config.get("STARTUP_CHECKS_ENABLED", True):
        return
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    with app.app_context():
        ok, checks = collect_startup_checks(app)

    if ok:
        return

    missing_keys = (checks.get("system_config") or {}).get("missing") or []
    path_issues = {k: v for k, v in (checks.get("paths") or {}).items() if v != "ok"}
    integration_issues = {
        k: v
        for k, v in (checks.get("integrations") or {}).items()
        if v not in ("ok", "disabled", "password_login_enabled")
    }

    msg_parts = []
    if missing_keys:
        msg_parts.append(f"missing_system_config={missing_keys}")
    if path_issues:
        msg_parts.append(f"path_issues={path_issues}")
    if integration_issues:
        msg_parts.append(f"integration_issues={integration_issues}")
    msg = (
        "Startup checks failed: " + "; ".join(msg_parts) if msg_parts else "Startup checks failed."
    )

    if app.config.get("STARTUP_CHECKS_FAIL_FAST"):
        raise RuntimeError(msg)
    try:
        app.logger.warning(msg)
    except Exception as log_exc:
        report_swallowed_exception(
            log_exc,
            context="db_startup.run_startup_checks.logger_warning",
            log_key="db_startup.run_startup_checks.logger_warning",
            log_window_seconds=300,
        )
