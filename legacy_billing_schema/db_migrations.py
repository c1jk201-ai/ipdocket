from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

from flask import current_app

from app.utils.error_logging import report_swallowed_exception

from .db_core import (
  DB_ERRORS,
  DatabaseError,
  _ensure_column,
  _execute_insert_returning_id,
  _invoice_table_prefix,
  _is_safe_identifier,
  _is_sqlite,
  _quote_ident,
  _table_exists,
  get_db,
  unified_clients_enabled,
)


def _ensure_init_db_invoice_columns(conn) -> None:
  """Make pre-existing facade invoice tables compatible with init_db indexes."""
  for column, column_type in [
    ("client_id", "INTEGER"),
    ("business_profile_id", "INTEGER DEFAULT 1"),
    ("number", "TEXT"),
    ("internal_reference", "TEXT"),
    ("invoice_type", "TEXT DEFAULT 'incoming'"),
    ("invoice_language", "TEXT DEFAULT 'en'"),
    ("issue_date", "TEXT"),
    ("due_date", "TEXT"),
    ("status", "TEXT DEFAULT 'draft'"),
    ("billing_status", "TEXT"),
    ("payment_status", "TEXT"),
    ("notes", "TEXT"),
    ("subtotal", "NUMERIC(15,2) DEFAULT 0"),
    ("tax", "NUMERIC(15,2) DEFAULT 0"),
    ("total", "NUMERIC(15,2) DEFAULT 0"),
    ("subtotal_minor", "INTEGER DEFAULT 0"),
    ("tax_minor", "INTEGER DEFAULT 0"),
    ("total_minor", "INTEGER DEFAULT 0"),
    ("currency", "TEXT"),
    ("vat_rate", "REAL"),
    ("business_snapshot", "TEXT"),
    ("payment_meta", "TEXT"),
    ("payment_verified", "INTEGER DEFAULT 0"),
    ("tax_issued_at", "TEXT"),
    ("tax_issue_type", "TEXT"),
    ("tax_issue_source", "TEXT"),
    ("tax_issue_note", "TEXT"),
    ("is_outgoing", "INTEGER DEFAULT 0"),
    ("settlement_meta", "TEXT"),
    ("internal_settlement_status", "TEXT"),
    ("internal_settlement_at", "TEXT"),
    ("ipm_case_id", "TEXT"),
    ("ipm_case_ref", "TEXT"),
    ("ipm_invoice_id", "TEXT"),
    ("is_deleted", "INTEGER DEFAULT 0"),
    ("deleted_at", "TEXT"),
    ("deleted_by", "INTEGER"),
    ("delete_reason", "TEXT"),
    ("deleted_op_id", "INTEGER"),
  ]:
    _ensure_column(conn, "invoices", column, column_type)


def init_db():
  conn = get_db()
  cur = conn.cursor()
  _ensure_init_db_invoice_columns(conn)
  script = """
    CREATE TABLE IF NOT EXISTS business_profile (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      sort_order INTEGER DEFAULT 0,
      address TEXT,
      email TEXT,
      phone TEXT,
      tax_id TEXT,
      currency TEXT,
      vat_rate REAL DEFAULT 0.0,
      next_invoice_no INTEGER DEFAULT 1,
      logo_path TEXT,
      bank_account TEXT
    );

    CREATE TABLE IF NOT EXISTS tax_invoice_profiles (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      tax_id TEXT,
      ceo_name TEXT,
      address TEXT,
      biz_type TEXT,
      biz_class TEXT,
      email TEXT,
      phone TEXT,
      is_default INTEGER DEFAULT 0,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS tax_invoice_drafts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invoice_id INTEGER,
      invoice_number TEXT,
      write_date TEXT,
      tax_type TEXT,
      issue_type TEXT,
      charge_direction TEXT,
      purpose_type TEXT,
      invoicee_type TEXT,
      invoicer_name TEXT,
      invoicer_corp_num TEXT,
      invoicee_name TEXT,
      invoicee_corp_num TEXT,
      supply_total NUMERIC(15,2) DEFAULT 0,
      tax_total NUMERIC(15,2) DEFAULT 0,
      total_amount NUMERIC(15,2) DEFAULT 0,
      memo TEXT,
      email_subject TEXT,
      form_json TEXT,
      status TEXT DEFAULT 'draft',
      issued_at TEXT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Global invoice numbering counters (system-wide, per date key)
    CREATE TABLE IF NOT EXISTS invoice_number_counters (
      date_key TEXT PRIMARY KEY,
      last_no INTEGER NOT NULL DEFAULT 0,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );


    CREATE TABLE IF NOT EXISTS clients (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT,
      phone TEXT,
      address TEXT,
      manager TEXT,
      notes TEXT,
      search_tags TEXT,
      ipm_party_id TEXT,
      ipm_client_id INTEGER,
      -- Business registration fields (optional)
      biz_reg_number TEXT,
      biz_company_name TEXT,
      biz_representative_name TEXT,
      biz_opening_date TEXT,
      biz_corp_registration_number TEXT,
      biz_business_location TEXT,
      biz_head_office_location TEXT,
      biz_business_type TEXT,
      biz_tax_invoice_email TEXT,
      is_deleted INTEGER DEFAULT 0,
      deleted_at TEXT,
      deleted_by INTEGER,
      delete_reason TEXT,
      deleted_op_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS invoices (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      client_id INTEGER NOT NULL,
      business_profile_id INTEGER DEFAULT 1,
      number TEXT,
      internal_reference TEXT,
      ipm_case_id TEXT,
      ipm_case_ref TEXT,
      ipm_invoice_id TEXT,
      issue_date TEXT,
      due_date TEXT,
      status TEXT DEFAULT 'draft',
      billing_status TEXT CHECK (billing_status IN ('draft','sent','void','tax_issued','cash_issued','processed','pre_overdue') OR billing_status IS NULL),
      payment_status TEXT CHECK (payment_status IN ('unpaid','pending','paid','none') OR payment_status IS NULL),
      notes TEXT,
      subtotal NUMERIC(15,2) DEFAULT 0,
      tax NUMERIC(15,2) DEFAULT 0,
      total NUMERIC(15,2) DEFAULT 0,
      subtotal_minor INTEGER DEFAULT 0,
      tax_minor INTEGER DEFAULT 0,
      total_minor INTEGER DEFAULT 0,
      currency TEXT,
      vat_rate REAL,
      business_snapshot TEXT,
      payment_meta TEXT,
      payment_verified INTEGER DEFAULT 0,
      tax_issued_at TEXT,
      tax_issue_type TEXT,
      tax_issue_source TEXT,
      tax_issue_note TEXT,
      is_outgoing INTEGER DEFAULT 0,
      settlement_meta TEXT,
      internal_settlement_status TEXT,
      internal_settlement_at TEXT,
      is_deleted INTEGER DEFAULT 0,
      deleted_at TEXT,
      deleted_by INTEGER,
      delete_reason TEXT,
      deleted_op_id INTEGER,
      UNIQUE (business_profile_id, number),
      FOREIGN KEY(client_id) REFERENCES clients(id),
      FOREIGN KEY(business_profile_id) REFERENCES business_profile(id)
    );

    CREATE TABLE IF NOT EXISTS line_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invoice_id INTEGER NOT NULL,
      description TEXT,
      qty REAL DEFAULT 1,
      unit_price REAL DEFAULT 0,
      item_type TEXT DEFAULT 'service',
      discount REAL DEFAULT 0,
      is_taxable INTEGER DEFAULT 1,
      qty_minor INTEGER,
      unit_price_minor INTEGER,
      phase TEXT,
      fx_currency TEXT,
      fx_fee REAL,
      fx_gov REAL,
      fx_markup REAL,
      fx_rate_used REAL,
      is_estimated INTEGER DEFAULT 0,
      FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
    );
	    CREATE INDEX IF NOT EXISTS idx_line_items_invoice_id ON line_items(invoice_id);
	    CREATE INDEX IF NOT EXISTS idx_line_items_invoice_type ON line_items(invoice_id, item_type);
	    CREATE INDEX IF NOT EXISTS idx_line_items_invoice_estimated ON line_items(invoice_id, is_estimated);

	    -- Invoice document revisions (print/PDF history)
	    CREATE TABLE IF NOT EXISTS invoice_revisions (
	      id INTEGER PRIMARY KEY AUTOINCREMENT,
	      invoice_id INTEGER NOT NULL,
	      revision_no INTEGER NOT NULL,
	      content_hash TEXT NOT NULL,
	      file_name TEXT,
	      render_lang TEXT,
	      render_outgoing INTEGER DEFAULT 0,
	      source TEXT,
	      snapshot_json TEXT NOT NULL,
	      created_by INTEGER NULL,
	      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	      FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
	      UNIQUE(invoice_id, revision_no)
	    );
	    CREATE INDEX IF NOT EXISTS idx_invoice_revisions_invoice_id ON invoice_revisions(invoice_id);
	    CREATE INDEX IF NOT EXISTS idx_invoice_revisions_hash ON invoice_revisions(invoice_id, content_hash);

	    CREATE TABLE IF NOT EXISTS invoice_templates (
	      id INTEGER PRIMARY KEY AUTOINCREMENT,
	      name TEXT NOT NULL UNIQUE,
	      description TEXT,
      default_items TEXT,
      payment_terms INTEGER DEFAULT 30,
      notes TEXT,
      business_profile_id INTEGER,
      language TEXT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (business_profile_id) REFERENCES business_profile(id)
    );

    CREATE TABLE IF NOT EXISTS template_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      template_id INTEGER NOT NULL,
      description TEXT NOT NULL,
      qty REAL DEFAULT 1,
      unit_price REAL DEFAULT 0,
      item_type TEXT CHECK(item_type IN ('service','admin')) DEFAULT 'service',
      discount REAL DEFAULT 0,
      is_taxable INTEGER DEFAULT 1,
      FOREIGN KEY (template_id) REFERENCES invoice_templates(id) ON DELETE CASCADE
    );



    -- Audit log table
    CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id TEXT,
      actor_id INTEGER NULL,
      user_id INTEGER NULL,
      action TEXT NOT NULL,
      target_type TEXT,
      target_id INTEGER,
      meta TEXT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS client_deposit_ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      business_profile_id INTEGER DEFAULT 1,
      client_id INTEGER NOT NULL,
      currency TEXT NOT NULL,
      amount_minor INTEGER NOT NULL,
      entry_type TEXT NOT NULL,
      memo TEXT,
      related_invoice_id INTEGER,
      related_entry_id INTEGER,
      related_bank_transaction_id TEXT,
      created_by INTEGER,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
      FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
      FOREIGN KEY(related_invoice_id) REFERENCES invoices(id) ON DELETE SET NULL,
      FOREIGN KEY(related_entry_id) REFERENCES client_deposit_ledger(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS idx_cdl_client_cur_created ON client_deposit_ledger(client_id, business_profile_id, currency, created_at);
    CREATE INDEX IF NOT EXISTS idx_cdl_related_invoice ON client_deposit_ledger(related_invoice_id);
    CREATE INDEX IF NOT EXISTS idx_cdl_related_bank_transaction_id ON client_deposit_ledger(related_bank_transaction_id);

    -- Client merge operation log with undo snapshots.
    CREATE TABLE IF NOT EXISTS client_merge_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      target_id INTEGER NOT NULL,
      sources_json TEXT NOT NULL,  -- JSON array of source client row snapshots.
      invoice_map_json TEXT NOT NULL, -- {invoice_id: source_client_id} map JSON.
      notes_appended TEXT,     -- Notes appended to the target, if any.
      merged_by INTEGER,      -- User ID that performed the operation.
      undone_at TIMESTAMP      -- Undo timestamp.
    );



    -- Invoice attachment table.
    CREATE TABLE IF NOT EXISTS invoice_attachments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invoice_id INTEGER NOT NULL,
      original_name TEXT NOT NULL,
      stored_name TEXT NOT NULL,
      content_type TEXT,
      size INTEGER,
      role TEXT DEFAULT 'general',
      uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      uploaded_by INTEGER,
      first_page_text TEXT,
      analysis_meta TEXT,
      FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_attach_invoice_id ON invoice_attachments(invoice_id);

    -- Client attachment table.
    CREATE TABLE IF NOT EXISTS client_attachments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      client_id INTEGER NOT NULL,
      original_name TEXT NOT NULL,
      stored_name TEXT NOT NULL,
      content_type TEXT,
      size INTEGER,
      uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      uploaded_by INTEGER,
      first_page_text TEXT,
      analysis_meta TEXT,
      FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_client_attach_client_id ON client_attachments(client_id);

    CREATE INDEX IF NOT EXISTS idx_invoices_issue_date ON invoices(issue_date);
    CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date);
    CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
    CREATE INDEX IF NOT EXISTS idx_invoices_billing_status ON invoices(billing_status);
    CREATE INDEX IF NOT EXISTS idx_invoices_payment_status ON invoices(payment_status);
    CREATE INDEX IF NOT EXISTS idx_invoices_tax_issue_type ON invoices(tax_issue_type);
    CREATE INDEX IF NOT EXISTS idx_invoices_bp ON invoices(business_profile_id);
    CREATE INDEX IF NOT EXISTS idx_invoices_total ON invoices(total);
    CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(number);
    CREATE INDEX IF NOT EXISTS idx_invoices_client_id ON invoices(client_id);
    -- Indexes for ipm integration columns will be handled in migrate_db
    -- to safely support existing databases where columns might be missing initially.
    CREATE INDEX IF NOT EXISTS idx_clients_name ON clients(name);



    -- Bank activity job cache
    CREATE TABLE IF NOT EXISTS bank_import_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      corp_num TEXT NOT NULL,
      bank_code TEXT NOT NULL,
      account_number TEXT NOT NULL,
      sdate TEXT NOT NULL, -- yyyyMMdd
      edate TEXT NOT NULL, -- yyyyMMdd
      job_id TEXT,
      job_state INTEGER,
      error_code INTEGER,
      error_reason TEXT,
      job_start_dt TEXT,
      job_end_dt TEXT,
      reg_dt TEXT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_import_jobs_key
      ON bank_import_jobs(corp_num, bank_code, account_number, sdate, edate);
    CREATE INDEX IF NOT EXISTS idx_bank_import_jobs_jobid ON bank_import_jobs(job_id);

    -- Bank activity transactions storage
    CREATE TABLE IF NOT EXISTS bank_transactions (
      tid TEXT PRIMARY KEY,
      corp_num TEXT,
      bank_code TEXT,
      account_number TEXT,
      account_name TEXT,
      currency TEXT,
      source_provider TEXT DEFAULT 'manual',
      external_id TEXT,
      trdate TEXT,  -- yyyyMMdd
      trdt TEXT,   -- yyyyMMddHHmmss or ISO
      trserial TEXT,
      acc_in INTEGER,
      acc_out INTEGER,
      balance INTEGER,
      remark1 TEXT,
      remark2 TEXT,
      remark3 TEXT,
      memo TEXT,
      tax_invoice_issued INTEGER DEFAULT 0,
      tax_invoice_issued_at TEXT,
      tax_invoice_override INTEGER,
      reg_dt TEXT,
      job_id TEXT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_eft_acc_trdt ON bank_transactions(bank_code, account_number, trdt);
    CREATE INDEX IF NOT EXISTS idx_eft_trdate ON bank_transactions(trdate);

    -- FX rates cache (source-scoped, e.g., 'sample')
    CREATE TABLE IF NOT EXISTS fx_rates_cache (
      source TEXT PRIMARY KEY,
      payload TEXT,
      fetched_at TEXT
    );

    -- ========================================
    -- Unified Invoice Integration Tables
    -- ========================================

    -- (A) Canonical matter link table (N:N) for unified app billing.
    CREATE TABLE IF NOT EXISTS invoice_case_map (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invoice_id INTEGER NOT NULL,
      case_id INTEGER NULL,      -- cases.id FK (Internal)
      matter_id TEXT NULL,      -- matter.matter_id (App)
      our_ref TEXT NULL,       -- Case reference string
      role TEXT DEFAULT 'primary',  -- 'primary', 'related', 'billing'
      is_deleted INTEGER DEFAULT 0,
      deleted_at TEXT,
      deleted_by INTEGER,
      delete_reason TEXT,
      deleted_op_id INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
      UNIQUE(invoice_id, case_id, matter_id)
    );
    CREATE INDEX IF NOT EXISTS idx_icm_invoice ON invoice_case_map(invoice_id);
    CREATE INDEX IF NOT EXISTS idx_icm_case ON invoice_case_map(case_id);
    CREATE INDEX IF NOT EXISTS idx_icm_matter ON invoice_case_map(matter_id);
    CREATE INDEX IF NOT EXISTS idx_icm_our_ref ON invoice_case_map(our_ref);

    -- (B) Canonical payment table replacing payment_meta JSON.
    CREATE TABLE IF NOT EXISTS invoice_payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invoice_id INTEGER NOT NULL,
      paid_at TEXT NOT NULL,
      amount_minor INTEGER NOT NULL, -- Integer amount in minor units.
      currency TEXT NOT NULL DEFAULT 'USD',
      method TEXT NULL,        -- 'bank', 'card', 'cash', 'deposit'
      reference TEXT NULL,      -- Transaction, bank, or processor ID.
      verified INTEGER NOT NULL DEFAULT 0,
      meta_json TEXT NULL,      -- Source payload data.
      created_by INTEGER NULL,
      is_deleted INTEGER DEFAULT 0,
      deleted_at TEXT,
      deleted_by INTEGER,
      delete_reason TEXT,
      deleted_op_id INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_ip_invoice ON invoice_payments(invoice_id);
    CREATE INDEX IF NOT EXISTS idx_ip_paid_at ON invoice_payments(paid_at);
    CREATE INDEX IF NOT EXISTS idx_ip_reference ON invoice_payments(reference);

    -- (C) Canonical external integration table for app, Stripe, Xero, and peers.
    CREATE TABLE IF NOT EXISTS invoice_integrations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invoice_id INTEGER NOT NULL,
      provider TEXT NOT NULL,     -- 'ipm', 'stripe', 'xero', 'legacy'
      external_invoice_id TEXT NULL,
      external_invoice_number TEXT NULL,
      external_invoice_url TEXT NULL,
      external_case_id TEXT NULL,   -- matter_id and similar identifiers.
      external_case_ref TEXT NULL,  -- our_ref and similar references.
      sync_status TEXT DEFAULT 'pending', -- 'pending', 'synced', 'error'
      last_synced_at TEXT NULL,
      meta_json TEXT NULL,      -- Provider-specific extra data.
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
      UNIQUE(provider, external_invoice_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ii_invoice ON invoice_integrations(invoice_id);
    CREATE INDEX IF NOT EXISTS idx_ii_provider ON invoice_integrations(provider);
    CREATE INDEX IF NOT EXISTS idx_ii_ext_number ON invoice_integrations(external_invoice_number);

    -- External invoice-case map (unprefixed, shared with main DB)
    CREATE TABLE IF NOT EXISTS external_invoice_case_map (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      matter_id TEXT NOT NULL,
      our_ref TEXT,
      external_invoice_id INTEGER NOT NULL,
      is_deleted INTEGER DEFAULT 0,
      deleted_at TEXT,
      deleted_by INTEGER,
      delete_reason TEXT,
      deleted_op_id INTEGER,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(matter_id, external_invoice_id)
    );
    CREATE INDEX IF NOT EXISTS idx_eicm_invoice_id ON external_invoice_case_map(external_invoice_id);
    CREATE INDEX IF NOT EXISTS idx_eicm_matter_id ON external_invoice_case_map(matter_id);

    -- ========================================
    -- Accounting (expenses / ledger)
    -- ========================================
    CREATE TABLE IF NOT EXISTS accounts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT UNIQUE,
      name TEXT NOT NULL,
      type TEXT NOT NULL,
      is_active INTEGER DEFAULT 1,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_accounts_type ON accounts(type);

    CREATE TABLE IF NOT EXISTS expense_categories (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT UNIQUE,
      name TEXT NOT NULL,
      account_id INTEGER NULL,
      vat_deductible INTEGER DEFAULT 1,
      is_default INTEGER DEFAULT 0,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(account_id) REFERENCES accounts(id)
    );
    CREATE INDEX IF NOT EXISTS idx_expense_categories_name ON expense_categories(name);

    CREATE TABLE IF NOT EXISTS expenses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      business_profile_id INTEGER NOT NULL DEFAULT 1,
      expense_date TEXT NOT NULL,
      vendor_name TEXT,
      vendor_tax_id TEXT,
      description TEXT,
      category_id INTEGER,
      currency TEXT DEFAULT 'USD',
      net_amount REAL DEFAULT 0,
      vat_amount REAL DEFAULT 0,
      total_amount REAL DEFAULT 0,
      tax_type TEXT DEFAULT 'tax_invoice',
      input_vat_eligible INTEGER DEFAULT 1,
      memo TEXT,
      is_deleted INTEGER DEFAULT 0,
      deleted_at TEXT,
      deleted_by INTEGER,
      delete_reason TEXT,
      deleted_op_id INTEGER,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
      FOREIGN KEY(category_id) REFERENCES expense_categories(id)
    );
    CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
    CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category_id);
    CREATE INDEX IF NOT EXISTS idx_expenses_bp ON expenses(business_profile_id);

    CREATE TABLE IF NOT EXISTS accounting_periods (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      business_profile_id INTEGER NOT NULL DEFAULT 1,
      period_type TEXT DEFAULT 'custom',
      start_date TEXT NOT NULL,
      end_date TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      notes TEXT,
      closed_at TIMESTAMP,
      closed_by INTEGER,
      reopened_at TIMESTAMP,
      reopened_by INTEGER,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      UNIQUE (business_profile_id, start_date, end_date),
      FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
      FOREIGN KEY(closed_by) REFERENCES users(id),
      FOREIGN KEY(reopened_by) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_accounting_periods_bp_dates ON accounting_periods(business_profile_id, start_date, end_date);
    CREATE INDEX IF NOT EXISTS idx_accounting_periods_status ON accounting_periods(status);

    CREATE TABLE IF NOT EXISTS journal_entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      entry_date TEXT NOT NULL,
      memo TEXT,
      business_profile_id INTEGER NOT NULL DEFAULT 1,
      source_type TEXT DEFAULT 'manual',
      source_id INTEGER,
      created_by INTEGER,
      approved INTEGER NOT NULL DEFAULT 0,
      approved_at TIMESTAMP,
      approved_by INTEGER,
      posted INTEGER NOT NULL DEFAULT 0,
      posted_at TIMESTAMP,
      posted_by INTEGER,
      reversed INTEGER NOT NULL DEFAULT 0,
      reversed_at TIMESTAMP,
      reversed_by INTEGER,
      reversal_of_entry_id INTEGER,
      reversed_by_entry_id INTEGER,
      locked_period INTEGER NOT NULL DEFAULT 0,
      locked_period_id INTEGER,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
      FOREIGN KEY(approved_by) REFERENCES users(id),
      FOREIGN KEY(posted_by) REFERENCES users(id),
      FOREIGN KEY(reversed_by) REFERENCES users(id),
      FOREIGN KEY(reversal_of_entry_id) REFERENCES journal_entries(id),
      FOREIGN KEY(reversed_by_entry_id) REFERENCES journal_entries(id),
      FOREIGN KEY(locked_period_id) REFERENCES accounting_periods(id)
    );
    CREATE INDEX IF NOT EXISTS idx_journal_entries_date ON journal_entries(entry_date);
    CREATE INDEX IF NOT EXISTS idx_journal_entries_source ON journal_entries(source_type, source_id);
    CREATE INDEX IF NOT EXISTS idx_journal_entries_posted_date ON journal_entries(posted, entry_date);
    CREATE INDEX IF NOT EXISTS idx_journal_entries_locked_period ON journal_entries(locked_period, locked_period_id);
    CREATE INDEX IF NOT EXISTS idx_journal_entries_reversal_of ON journal_entries(reversal_of_entry_id);

    CREATE TABLE IF NOT EXISTS journal_lines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      entry_id INTEGER NOT NULL,
      account_id INTEGER NOT NULL,
      debit REAL DEFAULT 0,
      credit REAL DEFAULT 0,
      currency TEXT DEFAULT 'USD',
      description TEXT,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE,
      FOREIGN KEY(account_id) REFERENCES accounts(id)
    );
    CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id);
    CREATE INDEX IF NOT EXISTS idx_journal_lines_account ON journal_lines(account_id);

    INSERT OR IGNORE INTO accounts (id, code, name, type, is_active)
    VALUES
      (1, '1010', 'Cash and Bank', 'asset', 1),
      (2, '1120', 'Accounts Receivable', 'asset', 1),
      (3, '2100', 'Accounts Payable', 'liability', 1),
      (4, '2200', 'Sales Tax Payable', 'liability', 1),
      (5, '2300', 'Sales Tax Receivable', 'asset', 1),
      (6, '4000', 'Revenue', 'revenue', 1),
      (7, '5000', 'Operating Expenses', 'expense', 1);

    INSERT OR IGNORE INTO expense_categories (id, code, name, account_id, vat_deductible, is_default)
    VALUES
      (1, 'OFFICE', 'Office Supplies', 7, 1, 1),
      (2, 'TRAVEL', 'Travel', 7, 1, 0),
      (3, 'MEAL', 'Meals and Entertainment', 7, 0, 0),
      (4, 'FEE', 'Fees', 7, 1, 0);
    """

  cur.executescript(script)
  conn.commit()
  conn.close()


def migrate_db():
  """Migrate the billing database schema."""
  conn = get_db()
  cur = conn.cursor()
  unified_clients = unified_clients_enabled()

  # Existing migrations.
  for table, column, column_type in [
    ("audit_log", "request_id", "TEXT"),
    ("audit_log", "actor_id", "INTEGER"),
    ("invoices", "currency", "TEXT"),
    ("invoices", "vat_rate", "REAL"),
    ("invoices", "business_snapshot", "TEXT"),
    ("clients", "manager", "TEXT"),
    ("clients", "phone", "TEXT"),
    ("clients", "address", "TEXT"),
    ("clients", "search_tags", "TEXT"),
    ("invoices", "admin_memo", "TEXT"),
    ("business_profile", "language", "TEXT DEFAULT 'en'"),
    ("business_profile", "sort_order", "INTEGER DEFAULT 0"),
    ("invoice_templates", "business_profile_id", "INTEGER"),
    ("invoice_templates", "language", "TEXT"),
    ("invoices", "language", "TEXT DEFAULT 'en'"),
    ("invoices", "is_outgoing", "INTEGER DEFAULT 0"),
    ("invoices", "tax_issued_at", "TEXT"),
    ("invoices", "tax_issue_type", "TEXT"),
    ("invoices", "tax_issue_source", "TEXT"),
    ("invoices", "tax_issue_note", "TEXT"),
    ("invoices", "internal_settlement_status", "TEXT"),
    ("invoices", "internal_settlement_at", "TEXT"),
    ("invoices", "ipm_case_id", "TEXT"),
    ("invoices", "ipm_case_ref", "TEXT"),
    ("invoices", "ipm_invoice_id", "TEXT"),
    ("invoices", "is_deleted", "INTEGER DEFAULT 0"),
    ("invoices", "deleted_at", "TEXT"),
    ("invoices", "deleted_by", "INTEGER"),
    ("invoices", "delete_reason", "TEXT"),
    ("invoices", "deleted_op_id", "INTEGER"),
    ("invoice_payments", "is_deleted", "INTEGER DEFAULT 0"),
    ("invoice_payments", "deleted_at", "TEXT"),
    ("invoice_payments", "deleted_by", "INTEGER"),
    ("invoice_payments", "delete_reason", "TEXT"),
    ("invoice_payments", "deleted_op_id", "INTEGER"),
    ("expenses", "is_deleted", "INTEGER DEFAULT 0"),
    ("expenses", "deleted_at", "TEXT"),
    ("expenses", "deleted_by", "INTEGER"),
    ("expenses", "delete_reason", "TEXT"),
    ("expenses", "deleted_op_id", "INTEGER"),
    ("invoice_case_map", "is_deleted", "INTEGER DEFAULT 0"),
    ("invoice_case_map", "deleted_at", "TEXT"),
    ("invoice_case_map", "deleted_by", "INTEGER"),
    ("invoice_case_map", "delete_reason", "TEXT"),
    ("invoice_case_map", "deleted_op_id", "INTEGER"),
    ("bank_transactions", "account_name", "TEXT"),
    ("bank_transactions", "currency", "TEXT"),
    ("bank_transactions", "source_provider", "TEXT DEFAULT 'manual'"),
    ("bank_transactions", "external_id", "TEXT"),
    ("external_invoice_case_map", "is_deleted", "INTEGER DEFAULT 0"),
    ("external_invoice_case_map", "deleted_at", "TEXT"),
    ("external_invoice_case_map", "deleted_by", "INTEGER"),
    ("external_invoice_case_map", "delete_reason", "TEXT"),
    ("external_invoice_case_map", "deleted_op_id", "INTEGER"),
  ]:
    if unified_clients and table == "clients":
      continue
    _ensure_column(conn, table, column, column_type)

  # Normalize sort_order for existing rows (safe if column doesn't exist yet)
  try:
    cur.execute("UPDATE business_profile SET sort_order=0 WHERE sort_order IS NULL")
  except DB_ERRORS:
    pass

  # App integration indexes (safe to run repeatedly as they are IF NOT EXISTS)
  for sql in [
    "CREATE INDEX IF NOT EXISTS idx_invoices_ipm_case_id ON invoices(ipm_case_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_ipm_case_ref ON invoices(ipm_case_ref)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_ipm_invoice_id ON invoices(ipm_invoice_id)",
  ]:
    try:
      cur.execute(sql)
    except DB_ERRORS:
      pass

  # Invoice revisions (print/PDF history)
  try:
    cur.executescript(
      """
      CREATE TABLE IF NOT EXISTS invoice_number_counters (
        date_key TEXT PRIMARY KEY,
        last_no INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );

      CREATE TABLE IF NOT EXISTS invoice_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        file_name TEXT,
        render_lang TEXT,
        render_outgoing INTEGER DEFAULT 0,
        source TEXT,
        snapshot_json TEXT NOT NULL,
        created_by INTEGER NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
        UNIQUE(invoice_id, revision_no)
      );
      CREATE INDEX IF NOT EXISTS idx_invoice_revisions_invoice_id ON invoice_revisions(invoice_id);
      CREATE INDEX IF NOT EXISTS idx_invoice_revisions_hash ON invoice_revisions(invoice_id, content_hash);
      """
    )
  except DB_ERRORS:
    pass

  # Ensure the merge log table exists.
  try:
    cur.execute(
      """
      CREATE TABLE IF NOT EXISTS client_merge_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        target_id INTEGER NOT NULL,
        sources_json TEXT NOT NULL,
        invoice_map_json TEXT NOT NULL,
        notes_appended TEXT,
        merged_by INTEGER,
        undone_at TIMESTAMP
      )
      """
    )
  except DB_ERRORS:
    pass

  # Bank matching: add tax-document display columns.
  for table, column, column_type in [
    ("bank_transactions", "tax_invoice_issued", "INTEGER DEFAULT 0"),
    ("bank_transactions", "tax_invoice_issued_at", "TEXT"),
    ("bank_transactions", "tax_invoice_override", "INTEGER"),
  ]:
    _ensure_column(conn, table, column, column_type)

  try:
    cur.executescript(
      """
      CREATE TABLE IF NOT EXISTS client_deposit_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_profile_id INTEGER DEFAULT 1,
        client_id INTEGER NOT NULL,
        currency TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,
        entry_type TEXT NOT NULL,
        memo TEXT,
        related_invoice_id INTEGER,
        related_entry_id INTEGER,
        related_bank_transaction_id TEXT,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(related_invoice_id) REFERENCES invoices(id) ON DELETE SET NULL,
        FOREIGN KEY(related_entry_id) REFERENCES client_deposit_ledger(id) ON DELETE SET NULL,
        FOREIGN KEY(created_by) REFERENCES users(id)
      );
      CREATE INDEX IF NOT EXISTS idx_cdl_client_cur_created ON client_deposit_ledger(client_id, business_profile_id, currency, created_at);
      CREATE INDEX IF NOT EXISTS idx_cdl_related_invoice ON client_deposit_ledger(related_invoice_id);
      CREATE INDEX IF NOT EXISTS idx_cdl_related_bank_transaction_id ON client_deposit_ledger(related_bank_transaction_id);
      """
    )
  except DB_ERRORS:
    pass

  # SQLite-only: allow global (business-profile-agnostic) deposits by making
  # client_deposit_ledger.business_profile_id nullable. SQLite can't ALTER COLUMN
  # constraints, so we rebuild the table when needed.
  try:
    if _is_sqlite(conn) and _table_exists(conn, "client_deposit_ledger"):
      prefix = _invoice_table_prefix()
      ledger_tbl = (
        f"{prefix}client_deposit_ledger"
        if current_app.config.get("INVOICEAPP_INTEGRATED") and prefix
        else "client_deposit_ledger"
      )
      if _is_safe_identifier(ledger_tbl):
        rows = conn.execute(f"PRAGMA table_info({_quote_ident(ledger_tbl)})").fetchall()
      else:
        rows = []

      notnull = None
      for r in rows or []:
        try:
          name = r["name"]
          nn = r["notnull"]
        except Exception:
          try:
            name = r[1]
            nn = r[3]
          except Exception:
            continue
        if str(name or "") == "business_profile_id":
          try:
            notnull = int(nn or 0)
          except Exception:
            notnull = 0
          break

      if notnull == 1 and _is_safe_identifier(ledger_tbl):
        old_tbl = f"{ledger_tbl}__old"
        if _is_safe_identifier(old_tbl):
          # Capture CREATE TABLE and index SQLs before renaming.
          row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (ledger_tbl,),
          ).fetchone()
          create_sql = None
          try:
            create_sql = row["sql"]
          except Exception:
            try:
              create_sql = row[0] if row else None
            except Exception:
              create_sql = None

          idx_rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (ledger_tbl,),
          ).fetchall()
          index_sqls = []
          for ir in idx_rows or []:
            try:
              s = ir["sql"]
            except Exception:
              try:
                s = ir[0]
              except Exception:
                s = None
            if s:
              index_sqls.append(str(s))

          if create_sql:
            # Remove NOT NULL constraint from business_profile_id column definition.
            new_create_sql = re.sub(
              r"(?i)(\bbusiness_profile_id\b[^,]*?)\bNOT\s+NULL\b",
              r"\1",
              str(create_sql),
              count=1,
            )

            conn.execute("PRAGMA foreign_keys=OFF;")
            try:
              conn.execute("BEGIN IMMEDIATE")
              # If a previous attempt left an old table behind, drop it.
              conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(old_tbl)}")

              conn.execute(
                f"ALTER TABLE {_quote_ident(ledger_tbl)} RENAME TO {_quote_ident(old_tbl)}"
              )
              conn.execute(new_create_sql)

              old_cols = [
                (r[1] if not hasattr(r, "keys") else r["name"])
                for r in conn.execute(
                  f"PRAGMA table_info({_quote_ident(old_tbl)})"
                ).fetchall()
              ]
              new_cols = [
                (r[1] if not hasattr(r, "keys") else r["name"])
                for r in conn.execute(
                  f"PRAGMA table_info({_quote_ident(ledger_tbl)})"
                ).fetchall()
              ]
              cols = [c for c in old_cols if c in set(new_cols)]
              if cols:
                cols_sql = ", ".join(_quote_ident(c) for c in cols)
                conn.execute(
                  f"INSERT INTO {_quote_ident(ledger_tbl)} ({cols_sql}) "
                  f"SELECT {cols_sql} FROM {_quote_ident(old_tbl)}"
                )

              conn.execute(f"DROP TABLE {_quote_ident(old_tbl)}")
              for s in index_sqls:
                try:
                  conn.execute(s)
                except Exception as exc:
                  report_swallowed_exception(
                    exc,
                    context="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.create_index",
                    log_key="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.create_index",
                    log_window_seconds=300,
                  )
              conn.execute("COMMIT")
            except Exception:
              try:
                conn.execute("ROLLBACK")
              except Exception as rollback_exc:
                report_swallowed_exception(
                  rollback_exc,
                  context="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.rollback",
                  log_key="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.rollback",
                  log_window_seconds=300,
                )
              # Best-effort recovery: if new table wasn't created, rename back.
              try:
                if (
                  not _table_exists(conn, "client_deposit_ledger")
                ) and _sqlite_table_exists_raw(conn, old_tbl):
                  conn.execute(
                    f"ALTER TABLE {_quote_ident(old_tbl)} RENAME TO {_quote_ident(ledger_tbl)}"
                  )
              except Exception as recover_exc:
                report_swallowed_exception(
                  recover_exc,
                  context="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.recover",
                  log_key="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.recover",
                  log_window_seconds=300,
                )
            finally:
              try:
                conn.execute("PRAGMA foreign_keys=ON;")
              except Exception as fk_exc:
                report_swallowed_exception(
                  fk_exc,
                  context="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.enable_foreign_keys",
                  log_key="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp.enable_foreign_keys",
                  log_window_seconds=300,
                )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp",
      log_key="billing_invoices.db_migrations.migrate_db.client_deposit_ledger_nullable_bp",
      log_window_seconds=300,
    )

  # Accounting tables (expenses / ledger)
  try:
    cur.executescript(
      """
      CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
      CREATE INDEX IF NOT EXISTS idx_accounts_type ON accounts(type);

      CREATE TABLE IF NOT EXISTS expense_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT NOT NULL,
        account_id INTEGER NULL,
        vat_deductible INTEGER DEFAULT 1,
        is_default INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(account_id) REFERENCES accounts(id)
      );
      CREATE INDEX IF NOT EXISTS idx_expense_categories_name ON expense_categories(name);

      CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_profile_id INTEGER NOT NULL DEFAULT 1,
        expense_date TEXT NOT NULL,
        vendor_name TEXT,
        vendor_tax_id TEXT,
        description TEXT,
        category_id INTEGER,
        currency TEXT DEFAULT 'USD',
        net_amount REAL DEFAULT 0,
        vat_amount REAL DEFAULT 0,
        total_amount REAL DEFAULT 0,
        tax_type TEXT DEFAULT 'tax_invoice',
        input_vat_eligible INTEGER DEFAULT 1,
        memo TEXT,
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT,
        deleted_by INTEGER,
        delete_reason TEXT,
        deleted_op_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
        FOREIGN KEY(category_id) REFERENCES expense_categories(id)
      );
      CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
      CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category_id);
      CREATE INDEX IF NOT EXISTS idx_expenses_bp ON expenses(business_profile_id);

      CREATE TABLE IF NOT EXISTS accounting_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_profile_id INTEGER NOT NULL DEFAULT 1,
        period_type TEXT DEFAULT 'custom',
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        notes TEXT,
        closed_at TIMESTAMP,
        closed_by INTEGER,
        reopened_at TIMESTAMP,
        reopened_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (business_profile_id, start_date, end_date),
        FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
        FOREIGN KEY(closed_by) REFERENCES users(id),
        FOREIGN KEY(reopened_by) REFERENCES users(id)
      );
      CREATE INDEX IF NOT EXISTS idx_accounting_periods_bp_dates ON accounting_periods(business_profile_id, start_date, end_date);
      CREATE INDEX IF NOT EXISTS idx_accounting_periods_status ON accounting_periods(status);

      CREATE TABLE IF NOT EXISTS journal_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        memo TEXT,
        business_profile_id INTEGER NOT NULL DEFAULT 1,
        source_type TEXT DEFAULT 'manual',
        source_id INTEGER,
        created_by INTEGER,
        approved INTEGER NOT NULL DEFAULT 0,
        approved_at TIMESTAMP,
        approved_by INTEGER,
        posted INTEGER NOT NULL DEFAULT 0,
        posted_at TIMESTAMP,
        posted_by INTEGER,
        reversed INTEGER NOT NULL DEFAULT 0,
        reversed_at TIMESTAMP,
        reversed_by INTEGER,
        reversal_of_entry_id INTEGER,
        reversed_by_entry_id INTEGER,
        locked_period INTEGER NOT NULL DEFAULT 0,
        locked_period_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
        FOREIGN KEY(approved_by) REFERENCES users(id),
        FOREIGN KEY(posted_by) REFERENCES users(id),
        FOREIGN KEY(reversed_by) REFERENCES users(id),
        FOREIGN KEY(reversal_of_entry_id) REFERENCES journal_entries(id),
        FOREIGN KEY(reversed_by_entry_id) REFERENCES journal_entries(id),
        FOREIGN KEY(locked_period_id) REFERENCES accounting_periods(id)
      );
      CREATE INDEX IF NOT EXISTS idx_journal_entries_date ON journal_entries(entry_date);
      CREATE INDEX IF NOT EXISTS idx_journal_entries_source ON journal_entries(source_type, source_id);
      CREATE INDEX IF NOT EXISTS idx_journal_entries_posted_date ON journal_entries(posted, entry_date);
      CREATE INDEX IF NOT EXISTS idx_journal_entries_locked_period ON journal_entries(locked_period, locked_period_id);
      CREATE INDEX IF NOT EXISTS idx_journal_entries_reversal_of ON journal_entries(reversal_of_entry_id);

      CREATE TABLE IF NOT EXISTS journal_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        debit REAL DEFAULT 0,
        credit REAL DEFAULT 0,
        currency TEXT DEFAULT 'USD',
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE,
        FOREIGN KEY(account_id) REFERENCES accounts(id)
      );
      CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id);
      CREATE INDEX IF NOT EXISTS idx_journal_lines_account ON journal_lines(account_id);
      """
    )
  except DB_ERRORS:
    pass

  # Seed default accounts/categories if empty
  try:
    row = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()
    acct_count = row[0] if row else 0
  except DB_ERRORS:
    acct_count = 0
  if not acct_count:
    try:
      cur.executemany(
        "INSERT INTO accounts (id, code, name, type, is_active) VALUES (?, ?, ?, ?, ?)",
        [
          (1, "1010", "Cash and Bank", "asset", 1),
          (2, "1120", "Accounts Receivable", "asset", 1),
          (3, "2100", "Accounts Payable", "liability", 1),
          (4, "2200", "Sales Tax Payable", "liability", 1),
          (5, "2300", "Sales Tax Receivable", "asset", 1),
          (6, "4000", "Revenue", "revenue", 1),
          (7, "5000", "Operating Expenses", "expense", 1),
        ],
      )
    except DB_ERRORS:
      pass

  try:
    row = cur.execute("SELECT COUNT(*) FROM expense_categories").fetchone()
    cat_count = row[0] if row else 0
  except DB_ERRORS:
    cat_count = 0
  if not cat_count:
    try:
      cur.executemany(
        "INSERT INTO expense_categories (id, code, name, account_id, vat_deductible, is_default) VALUES (?, ?, ?, ?, ?, ?)",
        [
          (1, "OFFICE", "Office Supplies", 7, 1, 1),
          (2, "TRAVEL", "Travel", 7, 1, 0),
          (3, "MEAL", "Meals and Entertainment", 7, 0, 0),
          (4, "FEE", "Fees", 7, 1, 0),
        ],
      )
    except DB_ERRORS:
      pass

  # Ensure line_items table exists (init_db might have been skipped/older)
  try:
    cur.execute(
      """
      CREATE TABLE IF NOT EXISTS line_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        description TEXT,
        qty REAL DEFAULT 1,
        unit_price REAL DEFAULT 0,
        item_type TEXT DEFAULT 'service',
        discount REAL DEFAULT 0,
        is_taxable INTEGER DEFAULT 1,
        qty_minor INTEGER,
        unit_price_minor INTEGER,
        phase TEXT,
        fx_currency TEXT,
        fx_fee REAL,
        fx_gov REAL,
        fx_markup REAL,
        fx_rate_used REAL,
        is_estimated INTEGER DEFAULT 0,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
      )
      """
    )
  except DB_ERRORS:
    pass

  for sql in [
    "CREATE INDEX IF NOT EXISTS idx_line_items_invoice_id ON line_items(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_line_items_invoice_type ON line_items(invoice_id, item_type)",
    "CREATE INDEX IF NOT EXISTS idx_line_items_invoice_estimated ON line_items(invoice_id, is_estimated)",
  ]:
    try:
      cur.execute(sql)
    except DB_ERRORS:
      pass

  # New migration: add minor-unit amount columns.
  _ensure_column(conn, "invoices", "subtotal_minor", "INTEGER")
  _ensure_column(conn, "invoices", "tax_minor", "INTEGER")
  _ensure_column(conn, "invoices", "total_minor", "INTEGER")
  _ensure_column(conn, "line_items", "qty_minor", "INTEGER")
  _ensure_column(conn, "line_items", "unit_price_minor", "INTEGER")

  # Add payment verification columns.
  _ensure_column(conn, "invoices", "payment_meta", "TEXT")
  _ensure_column(conn, "invoices", "payment_verified", "INTEGER DEFAULT 0")

  # Add settlement metadata columns for invoice split data.
  # Add settlement metadata columns for invoice split data.
  _ensure_column(conn, "invoices", "settlement_meta", "TEXT")

  # Add split status columns when missing.
  # Add split status columns when missing.
  _ensure_column(conn, "invoices", "billing_status", "TEXT")
  _ensure_column(conn, "invoices", "payment_status", "TEXT")
  _ensure_column(conn, "invoices", "tax_issue_type", "TEXT")
  _ensure_column(conn, "invoices", "tax_issue_source", "TEXT")
  _ensure_column(conn, "invoices", "tax_issue_note", "TEXT")

  # ERP-style accounting controls and period locking
  try:
    cur.execute(
      """
      CREATE TABLE IF NOT EXISTS accounting_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_profile_id INTEGER NOT NULL DEFAULT 1,
        period_type TEXT DEFAULT 'custom',
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        notes TEXT,
        closed_at TIMESTAMP,
        closed_by INTEGER,
        reopened_at TIMESTAMP,
        reopened_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (business_profile_id, start_date, end_date),
        FOREIGN KEY(business_profile_id) REFERENCES business_profile(id),
        FOREIGN KEY(closed_by) REFERENCES users(id),
        FOREIGN KEY(reopened_by) REFERENCES users(id)
      )
      """
    )
  except DB_ERRORS:
    pass

  approved_added = _ensure_column(
    conn, "journal_entries", "approved", "INTEGER NOT NULL DEFAULT 0"
  )
  _ensure_column(conn, "journal_entries", "approved_at", "TIMESTAMP")
  _ensure_column(conn, "journal_entries", "approved_by", "INTEGER")
  posted_added = _ensure_column(conn, "journal_entries", "posted", "INTEGER NOT NULL DEFAULT 0")
  _ensure_column(conn, "journal_entries", "posted_at", "TIMESTAMP")
  _ensure_column(conn, "journal_entries", "posted_by", "INTEGER")
  _ensure_column(conn, "journal_entries", "reversed", "INTEGER NOT NULL DEFAULT 0")
  _ensure_column(conn, "journal_entries", "reversed_at", "TIMESTAMP")
  _ensure_column(conn, "journal_entries", "reversed_by", "INTEGER")
  _ensure_column(conn, "journal_entries", "reversal_of_entry_id", "INTEGER")
  _ensure_column(conn, "journal_entries", "reversed_by_entry_id", "INTEGER")
  _ensure_column(conn, "journal_entries", "locked_period", "INTEGER NOT NULL DEFAULT 0")
  _ensure_column(conn, "journal_entries", "locked_period_id", "INTEGER")

  if approved_added or posted_added:
    try:
      cur.execute(
        """
        UPDATE journal_entries
          SET approved = 1,
            approved_at = COALESCE(approved_at, created_at),
            posted = 1,
            posted_at = COALESCE(posted_at, created_at)
         WHERE COALESCE(source_type, 'manual') <> 'reversal'
        """
      )
    except DB_ERRORS:
      pass

  for sql in [
    "CREATE INDEX IF NOT EXISTS idx_accounting_periods_bp_dates ON accounting_periods(business_profile_id, start_date, end_date)",
    "CREATE INDEX IF NOT EXISTS idx_accounting_periods_status ON accounting_periods(status)",
    "CREATE INDEX IF NOT EXISTS idx_journal_entries_posted_date ON journal_entries(posted, entry_date)",
    "CREATE INDEX IF NOT EXISTS idx_journal_entries_locked_period ON journal_entries(locked_period, locked_period_id)",
    "CREATE INDEX IF NOT EXISTS idx_journal_entries_reversal_of ON journal_entries(reversal_of_entry_id)",
  ]:
    try:
      cur.execute(sql)
    except DB_ERRORS:
      pass

  try:
    cur.execute(
      """
      UPDATE journal_entries AS je
        SET locked_period = CASE
          WHEN EXISTS (
            SELECT 1
             FROM accounting_periods ap
            WHERE ap.status = 'closed'
             AND ap.business_profile_id = je.business_profile_id
             AND ap.start_date <= je.entry_date
             AND ap.end_date >= je.entry_date
          ) THEN 1
          ELSE 0
        END
      """
    )
  except DB_ERRORS:
    pass

  try:
    cur.execute(
      """
      UPDATE journal_entries AS je
        SET locked_period_id = (
          SELECT ap.id
           FROM accounting_periods ap
          WHERE ap.status = 'closed'
           AND ap.business_profile_id = je.business_profile_id
           AND ap.start_date <= je.entry_date
           AND ap.end_date >= je.entry_date
          ORDER BY ap.end_date DESC, ap.id DESC
          LIMIT 1
        )
      """
    )
  except DB_ERRORS:
    pass

  # Clients: business registration fields (idempotent)
  for table, column, column_type in [
    ("clients", "biz_reg_number", "TEXT"),
    ("clients", "biz_company_name", "TEXT"),
    ("clients", "biz_representative_name", "TEXT"),
    ("clients", "biz_opening_date", "TEXT"),
    ("clients", "biz_corp_registration_number", "TEXT"),
    ("clients", "biz_business_location", "TEXT"),
    ("clients", "biz_head_office_location", "TEXT"),
    ("clients", "biz_business_type", "TEXT"),
    ("clients", "biz_tax_invoice_email", "TEXT"),
    ("clients", "ipm_party_id", "TEXT"),
    ("clients", "ipm_client_id", "INTEGER"),
    ("clients", "is_deleted", "INTEGER DEFAULT 0"),
    ("clients", "deleted_at", "TEXT"),
    ("clients", "deleted_by", "INTEGER"),
    ("clients", "delete_reason", "TEXT"),
    ("clients", "deleted_op_id", "INTEGER"),
  ]:
    if unified_clients:
      continue
    _ensure_column(conn, table, column, column_type)

  try:
    if unified_clients:
      raise DatabaseError()
    cur.execute(
      "CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_ipm_client_id "
      "ON clients(ipm_client_id) WHERE ipm_client_id IS NOT NULL"
    )
  except DB_ERRORS:
    pass

  # Ensure default business_profile exists (moved from init_db to support migration of sort_order)
  try:
    cur.execute(
      "INSERT OR IGNORE INTO business_profile (id, name, currency, vat_rate, next_invoice_no, sort_order) "
      "VALUES (1, 'My Company', 'USD', 0.0, 1, 0)"
    )
  except DB_ERRORS:
    pass

  # Ensure tax invoice profile tables & default row
  try:
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_tax_invoice_profiles_default ON tax_invoice_profiles(is_default)"
    )
  except DB_ERRORS:
    pass
  try:
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_tax_invoice_drafts_status ON tax_invoice_drafts(status)"
    )
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_tax_invoice_drafts_write_date ON tax_invoice_drafts(write_date)"
    )
  except DB_ERRORS:
    pass
  try:
    default_row = cur.execute(
      "SELECT id FROM tax_invoice_profiles WHERE is_default=1 LIMIT 1"
    ).fetchone()
    if not default_row:
      first_row = cur.execute(
        "SELECT id FROM tax_invoice_profiles ORDER BY id LIMIT 1"
      ).fetchone()
      if first_row:
        cur.execute(
          "UPDATE tax_invoice_profiles SET is_default=1 WHERE id=?",
          (first_row[0],),
        )
      else:
        bp_row = cur.execute(
          "SELECT name, address, email, phone, tax_id FROM business_profile "
          "ORDER BY COALESCE(sort_order, 0), id LIMIT 1"
        ).fetchone()
        if bp_row:
          cur.execute(
            "INSERT INTO tax_invoice_profiles (name, address, email, phone, tax_id, is_default) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (bp_row[0], bp_row[1], bp_row[2], bp_row[3], bp_row[4]),
          )
        else:
          cur.execute(
            "INSERT INTO tax_invoice_profiles (name, is_default) VALUES ('My Company', 1)"
          )
  except DB_ERRORS:
    pass

  try:
    if unified_clients:
      raise DatabaseError()
    cur.execute(
      "CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_ipm_party_id "
      "ON clients(ipm_party_id) WHERE ipm_party_id IS NOT NULL"
    )
  except DB_ERRORS:
    pass

  # Backfill once from legacy status/payment_verified values; ignore failures.
  try:
    cur.execute(
      """
      UPDATE invoices
      SET billing_status = CASE
        WHEN billing_status IS NOT NULL THEN billing_status
        WHEN status IN ('draft','sent','void','tax_issued','processed','pre_overdue') THEN status
        WHEN status IN ('payment_pending','paid') THEN 'sent'
        ELSE 'draft'
      END
      WHERE billing_status IS NULL
      """
    )
  except DB_ERRORS:
    pass

  try:
    cur.execute(
      """
      UPDATE invoices
      SET payment_status = CASE
        WHEN payment_status IS NOT NULL THEN payment_status
        -- NOTE: billing_status (e.g. tax_issued) and payment_status are independent.
        -- Do not assume tax_issued implies paid.
        WHEN status='paid' OR COALESCE(payment_verified,0)=1 THEN 'paid'
        WHEN status IN ('payment_pending','pre_overdue') THEN 'pending'
        WHEN status='void' THEN 'none'
        ELSE 'unpaid'
      END
      WHERE payment_status IS NULL
      """
    )
  except DB_ERRORS:
    pass

  # Ensure indexes.
  try:
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_invoices_billing_status ON invoices(billing_status)"
    )
  except DB_ERRORS:
    pass
  try:
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_invoices_payment_status ON invoices(payment_status)"
    )
  except DB_ERRORS:
    pass
  try:
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_invoices_tax_issue_type ON invoices(tax_issue_type)"
    )
  except DB_ERRORS:
    pass

  # Ensure app matter/expense link indexes.
  for sql in [
    "CREATE INDEX IF NOT EXISTS idx_invoices_ipm_case_id ON invoices(ipm_case_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_ipm_case_ref ON invoices(ipm_case_ref)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_ipm_invoice_id ON invoices(ipm_invoice_id)",
  ]:
    try:
      cur.execute(sql)
    except DB_ERRORS:
      pass

  # Ensure attachment tables.
  try:
    cur.execute(
      """
      CREATE TABLE IF NOT EXISTS invoice_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        content_type TEXT,
        size INTEGER,
        role TEXT DEFAULT 'general',
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        uploaded_by INTEGER,
        first_page_text TEXT,
        analysis_meta TEXT,
        FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
        FOREIGN KEY(uploaded_by) REFERENCES users(id)
      )
      """
    )
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_attach_invoice_id ON invoice_attachments(invoice_id)"
    )
    cur.execute(
      """
      CREATE TABLE IF NOT EXISTS client_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        content_type TEXT,
        size INTEGER,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        uploaded_by INTEGER,
        first_page_text TEXT,
        analysis_meta TEXT,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(uploaded_by) REFERENCES users(id)
      )
      """
    )
    cur.execute(
      "CREATE INDEX IF NOT EXISTS idx_client_attach_client_id ON client_attachments(client_id)"
    )
  except DB_ERRORS:
    pass

  # Add attachment analysis columns to existing databases.
  # Add attachment analysis columns to existing databases.
  _ensure_column(conn, "invoice_attachments", "role", "TEXT DEFAULT 'general'")
  _ensure_column(conn, "invoice_attachments", "first_page_text", "TEXT")
  _ensure_column(conn, "invoice_attachments", "analysis_meta", "TEXT")

  # Add index on invoices(client_id).
  try:
    cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_client_id ON invoices(client_id)")
  except DB_ERRORS:
    pass

  # Rebuild line_items with ON DELETE CASCADE.
  # Existing schemas may lack cascading foreign keys, so rebuild safely.
  if _is_sqlite(conn):
    try:
      cur.execute("PRAGMA foreign_keys=off;")
      cur.execute("BEGIN")
      cur.execute(
        """
        CREATE TABLE IF NOT EXISTS line_items_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          invoice_id INTEGER NOT NULL,
          description TEXT,
          qty REAL DEFAULT 1,
          unit_price REAL DEFAULT 0,
          item_type TEXT DEFAULT 'service',
          discount REAL DEFAULT 0,
          is_taxable INTEGER DEFAULT 1,
          qty_minor INTEGER,
          unit_price_minor INTEGER,
          phase TEXT,
          -- FX metadata (optional, used for 'foreign' items)
          fx_currency TEXT,
          fx_fee REAL,
          fx_gov REAL,
          fx_markup REAL,
          fx_rate_used REAL,
          is_estimated INTEGER DEFAULT 0,
          FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        );
        """
      )
      # Keep column ordering and mapping stable.
      cur.execute(
        """
        INSERT INTO line_items_new (id, invoice_id, description, qty, unit_price, item_type, discount, is_taxable, qty_minor, unit_price_minor,
                      phase, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used, is_estimated)
        SELECT id, invoice_id, description, qty, unit_price, item_type, discount, is_taxable,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'qty_minor') > 0 THEN qty_minor ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'unit_price_minor') > 0 THEN unit_price_minor ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'phase') > 0 THEN phase ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'fx_currency') > 0 THEN fx_currency ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'fx_fee') > 0 THEN fx_fee ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'fx_gov') > 0 THEN fx_gov ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'fx_markup') > 0 THEN fx_markup ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'fx_rate_used') > 0 THEN fx_rate_used ELSE NULL END,
            CASE WHEN instr((SELECT group_concat(name) FROM pragma_table_info('line_items')), 'is_estimated') > 0 THEN is_estimated ELSE 0 END
        FROM line_items;
        """
      )
      cur.execute("DROP TABLE line_items;")
      cur.execute("ALTER TABLE line_items_new RENAME TO line_items;")
      cur.execute("COMMIT")
    except DB_ERRORS:
      try:
        cur.execute("ROLLBACK")
      except DB_ERRORS:
        pass
    finally:
      try:
        cur.execute("PRAGMA foreign_keys=on;")
      except DB_ERRORS:
        pass

  # Ensure FX columns exist on existing line_items (idempotent fallback)
  _ensure_column(conn, "line_items", "fx_currency", "TEXT")
  _ensure_column(conn, "line_items", "fx_fee", "REAL")
  _ensure_column(conn, "line_items", "fx_gov", "REAL")
  _ensure_column(conn, "line_items", "fx_markup", "REAL")
  _ensure_column(conn, "line_items", "fx_rate_used", "REAL")
  _ensure_column(conn, "line_items", "phase", "TEXT")
  _ensure_column(conn, "line_items", "is_estimated", "INTEGER DEFAULT 0")
  _ensure_column(conn, "bank_transactions", "account_name", "TEXT")
  _ensure_column(conn, "bank_transactions", "currency", "TEXT")
  _ensure_column(conn, "bank_transactions", "source_provider", "TEXT DEFAULT 'manual'")
  _ensure_column(conn, "bank_transactions", "external_id", "TEXT")

  # Convert existing data to minor units.
  _migrate_amounts_to_minor(conn)

  # Ensure Bank activity tables exist for older DBs
  try:
    cur.executescript(
      """
      CREATE TABLE IF NOT EXISTS bank_import_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        corp_num TEXT NOT NULL,
        bank_code TEXT NOT NULL,
        account_number TEXT NOT NULL,
        sdate TEXT NOT NULL,
        edate TEXT NOT NULL,
        job_id TEXT,
        job_state INTEGER,
        error_code INTEGER,
        error_reason TEXT,
        job_start_dt TEXT,
        job_end_dt TEXT,
        reg_dt TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
      CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_import_jobs_key
        ON bank_import_jobs(corp_num, bank_code, account_number, sdate, edate);
      CREATE INDEX IF NOT EXISTS idx_bank_import_jobs_jobid ON bank_import_jobs(job_id);

      CREATE TABLE IF NOT EXISTS bank_transactions (
        tid TEXT PRIMARY KEY,
      corp_num TEXT,
      bank_code TEXT,
      account_number TEXT,
      account_name TEXT,
      currency TEXT,
      source_provider TEXT DEFAULT 'manual',
      external_id TEXT,
      trdate TEXT,
      trdt TEXT,
        trserial TEXT,
        acc_in INTEGER,
        acc_out INTEGER,
        balance INTEGER,
        remark1 TEXT,
        remark2 TEXT,
        remark3 TEXT,
        memo TEXT,
        tax_invoice_issued INTEGER DEFAULT 0,
        tax_invoice_issued_at TEXT,
        tax_invoice_override INTEGER,
        reg_dt TEXT,
        job_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
      CREATE INDEX IF NOT EXISTS idx_eft_acc_trdt ON bank_transactions(bank_code, account_number, trdt);
      CREATE INDEX IF NOT EXISTS idx_eft_trdate ON bank_transactions(trdate);

      -- FX rates cache table (idempotent)
      CREATE TABLE IF NOT EXISTS fx_rates_cache (
        source TEXT PRIMARY KEY,
        payload TEXT,
        fetched_at TEXT
      );

      -- Unified Invoice Integration Tables (idempotent)
      CREATE TABLE IF NOT EXISTS invoice_case_map (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        case_id INTEGER NULL,
        matter_id TEXT NULL,
        our_ref TEXT NULL,
        role TEXT DEFAULT 'primary',
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT,
        deleted_by INTEGER,
        delete_reason TEXT,
        deleted_op_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
        UNIQUE(invoice_id, case_id, matter_id)
      );
      CREATE INDEX IF NOT EXISTS idx_icm_invoice ON invoice_case_map(invoice_id);
      CREATE INDEX IF NOT EXISTS idx_icm_case ON invoice_case_map(case_id);
      CREATE INDEX IF NOT EXISTS idx_icm_matter ON invoice_case_map(matter_id);
      CREATE INDEX IF NOT EXISTS idx_icm_our_ref ON invoice_case_map(our_ref);

      CREATE TABLE IF NOT EXISTS invoice_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        paid_at TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        method TEXT NULL,
        reference TEXT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        meta_json TEXT NULL,
        created_by INTEGER NULL,
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT,
        deleted_by INTEGER,
        delete_reason TEXT,
        deleted_op_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS idx_ip_invoice ON invoice_payments(invoice_id);
      CREATE INDEX IF NOT EXISTS idx_ip_paid_at ON invoice_payments(paid_at);
      CREATE INDEX IF NOT EXISTS idx_ip_reference ON invoice_payments(reference);

      CREATE TABLE IF NOT EXISTS invoice_integrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        external_invoice_id TEXT NULL,
        external_invoice_number TEXT NULL,
        external_invoice_url TEXT NULL,
        external_case_id TEXT NULL,
        external_case_ref TEXT NULL,
        sync_status TEXT DEFAULT 'pending',
        last_synced_at TEXT NULL,
        meta_json TEXT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
        UNIQUE(provider, external_invoice_id)
      );
      CREATE INDEX IF NOT EXISTS idx_ii_invoice ON invoice_integrations(invoice_id);
      CREATE INDEX IF NOT EXISTS idx_ii_provider ON invoice_integrations(provider);
      CREATE INDEX IF NOT EXISTS idx_ii_ext_number ON invoice_integrations(external_invoice_number);

      CREATE TABLE IF NOT EXISTS external_invoice_case_map (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        matter_id TEXT NOT NULL,
        our_ref TEXT,
        external_invoice_id INTEGER NOT NULL,
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT,
        deleted_by INTEGER,
        delete_reason TEXT,
        deleted_op_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(matter_id, external_invoice_id)
      );
      CREATE INDEX IF NOT EXISTS idx_eicm_invoice_id ON external_invoice_case_map(external_invoice_id);
      CREATE INDEX IF NOT EXISTS idx_eicm_matter_id ON external_invoice_case_map(matter_id);
      """
    )
  except DB_ERRORS:
    pass

  if current_app.config.get("INVOICEAPP_UNIFIED_CLIENTS"):
    _warn_unified_clients_enabled()

  conn.commit()
  conn.close()


def _warn_unified_clients_enabled():
  logger.warning("Unified Client Mode is enabled. Legacy client tables are being migrated.")


def _sqlite_table_exists_raw(conn, table_name: str) -> bool:
  if not _is_sqlite(conn):
    return False
  try:
    row = conn.execute(
      "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return bool(row)
  except Exception:
    return False


def _sqlite_fk_targets(conn, table_name: str) -> set:
  if not _is_sqlite(conn):
    return set()
  if not _is_safe_identifier(table_name):
    return set()
  out = set()
  try:
    sql = f"PRAGMA foreign_key_list({_quote_ident(table_name)})"
    rows = conn.execute(sql).fetchall()
    for r in rows or []:
      try:
        out.add(r[2] if not hasattr(r, "keys") else r["table"])
      except Exception:
        continue
  except Exception:
    return set()
  return out


def _sqlite_table_create_sql(conn, table_name: str) -> str:
  if not _is_sqlite(conn):
    return ""
  try:
    row = conn.execute(
      "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
  except Exception:
    row = None
  if not row:
    return ""
  return (row[0] or "") if not hasattr(row, "keys") else (row["sql"] or "")


def _sqlite_table_index_sqls(conn, table_name: str) -> list:
  if not _is_sqlite(conn):
    return []
  try:
    rows = conn.execute(
      "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
      (table_name,),
    ).fetchall()
  except Exception:
    rows = []
  out = []
  for r in rows or []:
    s = (r[0] or "") if not hasattr(r, "keys") else (r["sql"] or "")
    if s:
      out.append(s)
  return out


def _rebuild_table_fk_swap(conn, table_name: str, from_table: str, to_table: str) -> None:
  if not _is_sqlite(conn):
    return
  if not (
    _is_safe_identifier(table_name)
    and _is_safe_identifier(from_table)
    and _is_safe_identifier(to_table)
  ):
    return
  if not _sqlite_table_exists_raw(conn, table_name):
    return

  fk_targets = _sqlite_fk_targets(conn, table_name)
  if to_table in fk_targets and from_table not in fk_targets:
    return

  create_sql = _sqlite_table_create_sql(conn, table_name)
  if not create_sql:
    return

  new_name = f"{table_name}__new"
  if not _is_safe_identifier(new_name):
    return
  if _sqlite_table_exists_raw(conn, new_name):
    try:
      sql = f"DROP TABLE {_quote_ident(new_name)}"
      conn.execute(sql)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_migrations._rebuild_table_fk_swap.drop_tmp_table",
        log_key="billing_invoices.db_migrations._rebuild_table_fk_swap.drop_tmp_table",
        log_window_seconds=300,
      )

  index_sqls = _sqlite_table_index_sqls(conn, table_name)
  new_sql = create_sql
  new_sql = re.sub(
    rf"(?i)(\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?)({re.escape(table_name)})(\b)",
    lambda m: f"{m.group(1)}{new_name}{m.group(3)}",
    new_sql,
    count=1,
  )
  new_sql = re.sub(
    rf"(?i)(\breferences\s+)({re.escape(from_table)})(\b)",
    lambda m: f"{m.group(1)}{to_table}{m.group(3)}",
    new_sql,
  )
  conn.execute(new_sql)

  old_cols = [
    (r[1] if not hasattr(r, "keys") else r["name"])
    for r in conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()
  ]
  sql = f"PRAGMA table_info({_quote_ident(new_name)})"
  new_cols = [
    (r[1] if not hasattr(r, "keys") else r["name"]) for r in conn.execute(sql).fetchall()
  ]
  cols = [c for c in old_cols if c in set(new_cols)]
  if cols:
    cols_sql = ", ".join(_quote_ident(c) for c in cols)
    conn.execute(
      f"INSERT INTO {_quote_ident(new_name)} ({cols_sql}) "
      f"SELECT {cols_sql} FROM {_quote_ident(table_name)}"
    )

  sql = f"DROP TABLE {_quote_ident(table_name)}"
  conn.execute(sql)
  conn.execute(f"ALTER TABLE {_quote_ident(new_name)} RENAME TO {_quote_ident(table_name)}")
  for s in index_sqls:
    try:
      conn.execute(s)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_migrations._rebuild_table_fk_swap.create_index",
        log_key="billing_invoices.db_migrations._rebuild_table_fk_swap.create_index",
        log_window_seconds=300,
      )


def _migrate_unified_clients(conn) -> None:
  if not _is_sqlite(conn):
    return
  if not current_app.config.get("INVOICEAPP_INTEGRATED"):
    return
  prefix = _invoice_table_prefix()
  if not prefix:
    return

  legacy_clients = f"{prefix}clients"
  invoices_tbl = f"{prefix}invoices"
  ledger_tbl = f"{prefix}client_deposit_ledger"
  client_attach_tbl = f"{prefix}client_attachments"
  merge_log_tbl = f"{prefix}client_merge_log"

  if not _sqlite_table_exists_raw(conn, legacy_clients):
    return

  try:
    conn.execute("PRAGMA foreign_keys=OFF")
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_migrations._migrate_unified_clients.disable_foreign_keys",
      log_key="billing_invoices.db_migrations._migrate_unified_clients.disable_foreign_keys",
      log_window_seconds=300,
    )

  try:
    rows = conn.execute(
      f"SELECT id, ipm_client_id, name, email, phone, address, manager, notes, biz_reg_number, biz_company_name, biz_representative_name, biz_opening_date, biz_corp_registration_number, biz_business_location, biz_head_office_location, biz_business_type, biz_tax_invoice_email, ipm_party_id FROM {legacy_clients}"
    ).fetchall()
  except Exception:
    rows = []

  for r in rows or []:
    legacy_id = r[0] if not hasattr(r, "keys") else r["id"]
    crm_id = r[1] if not hasattr(r, "keys") else r["ipm_client_id"]

    if crm_id is None:
      try:
        existing = conn.execute(
          "SELECT id FROM clients WHERE external_invoice_client_id=?",
          (legacy_id,),
        ).fetchone()
      except Exception:
        existing = None
      if existing is not None:
        crm_id = existing[0] if not hasattr(existing, "keys") else existing["id"]
        try:
          conn.execute(
            f"UPDATE {legacy_clients} SET ipm_client_id=? WHERE id=?",
            (crm_id, legacy_id),
          )
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.db_migrations._migrate_unified_clients.update_legacy_clients_ipm_client_id",
            log_key="billing_invoices.db_migrations._migrate_unified_clients.update_legacy_clients_ipm_client_id",
            log_window_seconds=300,
          )
      else:
        name = r[2] if not hasattr(r, "keys") else r["name"]
        email = r[3] if not hasattr(r, "keys") else r["email"]
        phone = r[4] if not hasattr(r, "keys") else r["phone"]
        address = r[5] if not hasattr(r, "keys") else r["address"]
        manager = r[6] if not hasattr(r, "keys") else r["manager"]
        notes = r[7] if not hasattr(r, "keys") else r["notes"]
        biz_reg_number = r[8] if not hasattr(r, "keys") else r["biz_reg_number"]
        biz_company_name = r[9] if not hasattr(r, "keys") else r["biz_company_name"]
        biz_representative_name = (
          r[10] if not hasattr(r, "keys") else r["biz_representative_name"]
        )
        biz_opening_date = r[11] if not hasattr(r, "keys") else r["biz_opening_date"]
        biz_corp_registration_number = (
          r[12] if not hasattr(r, "keys") else r["biz_corp_registration_number"]
        )
        biz_business_location = (
          r[13] if not hasattr(r, "keys") else r["biz_business_location"]
        )
        biz_head_office_location = (
          r[14] if not hasattr(r, "keys") else r["biz_head_office_location"]
        )
        biz_business_type = r[15] if not hasattr(r, "keys") else r["biz_business_type"]
        biz_tax_invoice_email = (
          r[16] if not hasattr(r, "keys") else r["biz_tax_invoice_email"]
        )
        ipm_party_id = r[17] if not hasattr(r, "keys") else r["ipm_party_id"]

        if ipm_party_id:
          try:
            existing_party = conn.execute(
              "SELECT id FROM clients WHERE ipm_party_id=?",
              (ipm_party_id,),
            ).fetchone()
          except Exception:
            existing_party = None
          if existing_party is not None:
            crm_id = (
              existing_party[0]
              if not hasattr(existing_party, "keys")
              else existing_party["id"]
            )
            try:
              conn.execute(
                f"UPDATE {legacy_clients} SET ipm_client_id=? WHERE id=?",
                (crm_id, legacy_id),
              )
            except Exception as exc:
              report_swallowed_exception(
                exc,
                context="billing_invoices.db_migrations._migrate_unified_clients.update_legacy_clients_ipm_client_id",
                log_key="billing_invoices.db_migrations._migrate_unified_clients.update_legacy_clients_ipm_client_id",
                log_window_seconds=300,
              )
            continue

        try:
          crm_id = _execute_insert_returning_id(
            conn,
            """
            INSERT INTO clients (
              name, email, phone, address, manager, notes,
              biz_reg_number, biz_company_name, biz_representative_name, biz_opening_date,
              biz_corp_registration_number, biz_business_location, biz_head_office_location,
              biz_business_type, biz_tax_invoice_email,
              ipm_party_id, external_invoice_client_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
              name,
              email,
              phone,
              address,
              manager,
              notes,
              biz_reg_number,
              biz_company_name,
              biz_representative_name,
              biz_opening_date,
              biz_corp_registration_number,
              biz_business_location,
              biz_head_office_location,
              biz_business_type,
              biz_tax_invoice_email,
              ipm_party_id,
              legacy_id,
            ),
          )
        except Exception:
          crm_id = None

        if crm_id is not None:
          try:
            conn.execute(
              f"UPDATE {legacy_clients} SET ipm_client_id=? WHERE id=?",
              (crm_id, legacy_id),
            )
          except Exception as exc:
            report_swallowed_exception(
              exc,
              context="billing_invoices.db_migrations._migrate_unified_clients.update_legacy_clients_ipm_client_id",
              log_key="billing_invoices.db_migrations._migrate_unified_clients.update_legacy_clients_ipm_client_id",
              log_window_seconds=300,
            )

    if crm_id is not None:
      try:
        conn.execute(
          "UPDATE clients SET external_invoice_client_id=COALESCE(external_invoice_client_id, ?) WHERE id=?",
          (legacy_id, crm_id),
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.db_migrations._migrate_unified_clients.update_clients_external_invoice_client_id",
          log_key="billing_invoices.db_migrations._migrate_unified_clients.update_clients_external_invoice_client_id",
          log_window_seconds=300,
        )

  try:
    conn.execute(
      """
      UPDATE clients
        SET ipm_client_id=id
       WHERE ipm_client_id IS NULL
        AND id NOT IN (SELECT ipm_client_id FROM clients WHERE ipm_client_id IS NOT NULL)
      """
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_migrations._migrate_unified_clients.backfill_ipm_client_id",
      log_key="billing_invoices.db_migrations._migrate_unified_clients.backfill_ipm_client_id",
      log_window_seconds=300,
    )

  try:
    conn.execute(
      f"""
      UPDATE clients
        SET manager = COALESCE(NULLIF(manager,''), (SELECT manager FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          notes = COALESCE(NULLIF(notes,''), (SELECT notes FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_reg_number = COALESCE(NULLIF(biz_reg_number,''), (SELECT biz_reg_number FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_company_name = COALESCE(NULLIF(biz_company_name,''), (SELECT biz_company_name FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_representative_name = COALESCE(NULLIF(biz_representative_name,''), (SELECT biz_representative_name FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_opening_date = COALESCE(NULLIF(biz_opening_date,''), (SELECT biz_opening_date FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_corp_registration_number = COALESCE(NULLIF(biz_corp_registration_number,''), (SELECT biz_corp_registration_number FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_business_location = COALESCE(NULLIF(biz_business_location,''), (SELECT biz_business_location FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_head_office_location = COALESCE(NULLIF(biz_head_office_location,''), (SELECT biz_head_office_location FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_business_type = COALESCE(NULLIF(biz_business_type,''), (SELECT biz_business_type FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          biz_tax_invoice_email = COALESCE(NULLIF(biz_tax_invoice_email,''), (SELECT biz_tax_invoice_email FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1)),
          ipm_party_id = COALESCE(NULLIF(ipm_party_id,''), (SELECT ipm_party_id FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id LIMIT 1))
       WHERE EXISTS (SELECT 1 FROM {legacy_clients} bc WHERE bc.ipm_client_id = clients.id)
      """
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_migrations._migrate_unified_clients.backfill_client_fields",
      log_key="billing_invoices.db_migrations._migrate_unified_clients.backfill_client_fields",
      log_window_seconds=300,
    )

  if _sqlite_table_exists_raw(conn, invoices_tbl):
    try:
      needs = conn.execute(
        f"""
        SELECT COUNT(*)
         FROM {invoices_tbl} i
         LEFT JOIN clients c ON c.id = i.client_id
         WHERE c.id IS NULL
          AND EXISTS (
            SELECT 1 FROM {legacy_clients} bc
             WHERE bc.id = i.client_id AND bc.ipm_client_id IS NOT NULL
          )
        """
      ).fetchone()[0]
    except Exception:
      needs = 0

    if int(needs or 0) > 0:
      try:
        conn.execute(
          f"""
          UPDATE {invoices_tbl}
            SET client_id = (SELECT ipm_client_id FROM {legacy_clients} bc WHERE bc.id = {invoices_tbl}.client_id)
           WHERE EXISTS (SELECT 1 FROM {legacy_clients} bc WHERE bc.id = {invoices_tbl}.client_id AND bc.ipm_client_id IS NOT NULL)
          """
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.db_migrations._migrate_unified_clients.rewire_invoices_client_id",
          log_key="billing_invoices.db_migrations._migrate_unified_clients.rewire_invoices_client_id",
          log_window_seconds=300,
        )

  if _sqlite_table_exists_raw(conn, ledger_tbl):
    try:
      needs = conn.execute(
        f"""
        SELECT COUNT(*)
         FROM {ledger_tbl} l
         LEFT JOIN clients c ON c.id = l.client_id
         WHERE c.id IS NULL
          AND EXISTS (
            SELECT 1 FROM {legacy_clients} bc
             WHERE bc.id = l.client_id AND bc.ipm_client_id IS NOT NULL
          )
        """
      ).fetchone()[0]
    except Exception:
      needs = 0

    if int(needs or 0) > 0:
      try:
        conn.execute(
          f"""
          UPDATE {ledger_tbl}
            SET client_id = (SELECT ipm_client_id FROM {legacy_clients} bc WHERE bc.id = {ledger_tbl}.client_id)
           WHERE EXISTS (SELECT 1 FROM {legacy_clients} bc WHERE bc.id = {ledger_tbl}.client_id AND bc.ipm_client_id IS NOT NULL)
          """
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.db_migrations._migrate_unified_clients.rewire_ledger_client_id",
          log_key="billing_invoices.db_migrations._migrate_unified_clients.rewire_ledger_client_id",
          log_window_seconds=300,
        )

  if _sqlite_table_exists_raw(conn, client_attach_tbl):
    try:
      needs = conn.execute(
        f"""
        SELECT COUNT(*)
         FROM {client_attach_tbl} a
         LEFT JOIN clients c ON c.id = a.client_id
         WHERE c.id IS NULL
          AND EXISTS (
            SELECT 1 FROM {legacy_clients} bc
             WHERE bc.id = a.client_id AND bc.ipm_client_id IS NOT NULL
          )
        """
      ).fetchone()[0]
    except Exception:
      needs = 0

    if int(needs or 0) > 0:
      try:
        conn.execute(
          f"""
          UPDATE {client_attach_tbl}
            SET client_id = (SELECT ipm_client_id FROM {legacy_clients} bc WHERE bc.id = {client_attach_tbl}.client_id)
           WHERE EXISTS (SELECT 1 FROM {legacy_clients} bc WHERE bc.id = {client_attach_tbl}.client_id AND bc.ipm_client_id IS NOT NULL)
          """
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.db_migrations._migrate_unified_clients.rewire_client_attachments_client_id",
          log_key="billing_invoices.db_migrations._migrate_unified_clients.rewire_client_attachments_client_id",
          log_window_seconds=300,
        )

  if _sqlite_table_exists_raw(conn, merge_log_tbl):
    try:
      needs = conn.execute(
        f"""
        SELECT COUNT(*)
         FROM {merge_log_tbl} m
         LEFT JOIN clients c ON c.id = m.target_id
         WHERE c.id IS NULL
          AND EXISTS (
            SELECT 1 FROM {legacy_clients} bc
             WHERE bc.id = m.target_id AND bc.ipm_client_id IS NOT NULL
          )
        """
      ).fetchone()[0]
    except Exception:
      needs = 0

    if int(needs or 0) > 0:
      try:
        conn.execute(
          f"""
          UPDATE {merge_log_tbl}
            SET target_id = (SELECT ipm_client_id FROM {legacy_clients} bc WHERE bc.id = {merge_log_tbl}.target_id)
           WHERE EXISTS (SELECT 1 FROM {legacy_clients} bc WHERE bc.id = {merge_log_tbl}.target_id AND bc.ipm_client_id IS NOT NULL)
          """
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.db_migrations._migrate_unified_clients.rewire_merge_log_target_id",
          log_key="billing_invoices.db_migrations._migrate_unified_clients.rewire_merge_log_target_id",
          log_window_seconds=300,
        )

  _rebuild_table_fk_swap(conn, invoices_tbl, legacy_clients, "clients")
  _rebuild_table_fk_swap(conn, ledger_tbl, legacy_clients, "clients")
  _rebuild_table_fk_swap(conn, client_attach_tbl, legacy_clients, "clients")

  try:
    conn.execute("PRAGMA foreign_keys=ON")
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_migrations._migrate_unified_clients.enable_foreign_keys",
      log_key="billing_invoices.db_migrations._migrate_unified_clients.enable_foreign_keys",
      log_window_seconds=300,
    )


def _migrate_amounts_to_minor(conn):
  """Migrate legacy float amounts to integer minor units."""
  from decimal import Decimal

  from app.services.billing.utils import to_minor

  cur = conn.cursor()

  if not _table_exists(conn, "invoices"):
    return
  if not _table_exists(conn, "line_items"):
    return

  # Migrate invoices.
  invoices = cur.execute(
    """
    SELECT id, subtotal, tax, total, currency
    FROM invoices
    WHERE subtotal_minor IS NULL OR tax_minor IS NULL OR total_minor IS NULL
    """
  ).fetchall()

  for inv in invoices:
    inv_id = inv[0]
    currency = inv[4] or "USD"

    subtotal = Decimal(str(inv[1] or 0))
    tax = Decimal(str(inv[2] or 0))
    total = Decimal(str(inv[3] or 0))

    subtotal_minor = to_minor(subtotal, currency)
    tax_minor = to_minor(tax, currency)
    total_minor = to_minor(total, currency)

    cur.execute(
      "UPDATE invoices SET subtotal_minor=?, tax_minor=?, total_minor=? WHERE id=?",
      (subtotal_minor, tax_minor, total_minor, inv_id),
    )

  # Migrate line items.
  items = cur.execute(
    """SELECT li.id, li.qty, li.unit_price, i.currency
      FROM line_items li
      JOIN invoices i ON i.id = li.invoice_id
      WHERE li.qty_minor IS NULL OR li.unit_price_minor IS NULL"""
  ).fetchall()

  for item in items:
    item_id = item[0]
    currency = item[3] or "USD"

    qty = Decimal(str(item[1] or 0))
    unit_price = Decimal(str(item[2] or 0))

    qty_minor = to_minor(qty, currency)
    unit_price_minor = to_minor(unit_price, currency)

    cur.execute(
      "UPDATE line_items SET qty_minor=?, unit_price_minor=? WHERE id=?",
      (qty_minor, unit_price_minor, item_id),
    )
