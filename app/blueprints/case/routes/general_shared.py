from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf
from sqlalchemy import and_, desc, func, inspect, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.blueprints.case import bp
from app.blueprints.case.forms import CaseForm
from app.blueprints.case.services.detail_context import build_case_detail_context
from app.extensions import db
from app.models.case import Case
from app.models.case_details import (
    CaseDesign,
    CaseForeignInfo,
    CaseLitigation,
    CasePatent,
    CaseTrademark,
)
from app.models.case_flat_index import CaseFlatIndex
from app.models.client import Client
from app.models.deadline import Deadline, RenewalFee
from app.models.ip_records import (
    AnnuityItem,
    DocketItem,
    ExternalInvoiceCaseLink,
    Family,
    FileAsset,
    Matter,
    MatterCustomField,
    MatterFamily,
    MatterFileAsset,
    MatterIdentifier,
    MatterMemo,
    LegacyExpense,
    LegacyExpensePayment,
    LegacyInvoice,
    LegacyInvoicePayment,
    RawImportField,
    VMatterOverview,
)
from app.models.system_config import SystemConfig
from app.models.user import User
from app.models.workflow import Workflow
from app.services.automation.edit_recommendation import EditRecommendationService
from app.services.billing.invoice_bridge import InvoiceBridgeError, fetch_linked_invoices_for_case
from app.services.billing.invoice_matter_link_usecase import InvoiceMatterLinkUseCase
from app.services.case.canonical_field_service import upsert_case_flat_index
from app.services.case.case_parameter_service import CaseParameterService
from app.services.client.client_party_sync import ensure_clients_synced_from_party
from app.services.core.staff_options import build_staff_assignment_lists
from app.services.deadlines.docket_service import (
    complete_exam_request_docket,
    complete_filing_docket,
    complete_registration_docket,
    upsert_exam_request_docket,
    upsert_filing_docket,
    upsert_registration_docket,
)
from app.services.matter.matter_auto_status import date_only_str as _svc_date_only_str
from app.services.matter.matter_auto_status import (
    derive_auto_status,
    has_supporting_red_signal,
    is_known_deadline_red_label,
)
from app.services.matter.matter_domain import (
    MatterCreateCommand,
    MatterCreatePrepareCommand,
    MatterCreateResult,
)
from app.services.parameter_conflict.parameter_conflict_resolver import ParameterConflictResolver
from app.services.uploads.upload_validation import (
    ALLOWED_EMAIL_EXTS,
    filter_upload_files,
)
from app.services.uploads.zip_safety import ZipSafetyError, safe_extract_bytes, safe_list
from app.services.workflow.task_sync import sync_from_docket_item
from app.utils.api_response import api_response
from app.utils.case_dto import matter_summary
from app.utils.permissions import matter_action
from app.utils.policy_sql import policy_text as text
from config import Config

from ..helpers import *

__all__ = [name for name in globals() if not name.startswith("__")]
