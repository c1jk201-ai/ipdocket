"""
Deadline Notification Service

Email notification system for deadline reminders.
"""

from __future__ import annotations

import json
import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from flask import current_app, has_app_context
from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.matter_facts import MatterFacts
from app.models.notification import NotificationLog
from app.models.ip_records import AnnuityItem, DocketItem, Matter
from app.models.user import User
from app.services.core.config_service import ConfigService
from app.utils.docket_dates import (
    effective_due_for_work,
    effective_due_text_expr,
    normalize_date_str,
)
from app.utils.docket_visibility import is_visible_by_date, visible_on_or_before
from app.utils.error_logging import report_swallowed_exception
from app.utils.renewal_labels import (
    normalize_renewal_jurisdiction,
    normalize_renewal_right_type,
    renewal_workflow_name,
)

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}
_CHANNEL_DISABLED_WARNED: set[str] = set()


# Fallback default reminder schedule (days before deadline)
FALLBACK_REMINDER_DAYS = [30, 14, 7, 1]
FALLBACK_ANNUITY_REMINDER_DAYS = [60, 30, 14]
ANNUITY_REMINDER_DUE_BASIS_KEY = "DEADLINE_ANNUITY_REMINDER_DUE_BASIS"
ANNUITY_REMINDER_DUE_BASIS_LEGAL = "legal"
ANNUITY_REMINDER_DUE_BASIS_EFFECTIVE = "effective"



def get_reminder_days_from_config() -> list[int]:
    """Get reminder days from SystemConfig, with fallback to defaults."""
    raw = ConfigService.get_str("DEADLINE_REMINDER_DAYS", "30,14,7,1", allow_blank=False) or ""
    try:
        return [int(d.strip()) for d in raw.split(",") if d.strip()]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline_notifications.reminder_days.parse",
            log_key="deadline_notifications.reminder_days.parse",
            log_window_seconds=300,
        )
        return FALLBACK_REMINDER_DAYS


def get_annuity_reminder_days_from_config() -> list[int]:
    """Get annuity reminder days from SystemConfig."""
    raw = (
        ConfigService.get_str("DEADLINE_ANNUITY_REMINDER_DAYS", "60,30,14", allow_blank=False) or ""
    )
    try:
        return [int(d.strip()) for d in raw.split(",") if d.strip()]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline_notifications.annuity_reminder_days.parse",
            log_key="deadline_notifications.annuity_reminder_days.parse",
            log_window_seconds=300,
        )
        return FALLBACK_ANNUITY_REMINDER_DAYS


def _annuity_reminder_due_basis() -> str:
    """
    Resolve annuity reminder date basis.

    - legal: StatutoryDeadline(extended_due_date > due_date) 
    - effective: InternalDeadline/StatutoryDeadline In Progress   value 
    """
    raw = (
        ConfigService.get_str(
            ANNUITY_REMINDER_DUE_BASIS_KEY,
            ANNUITY_REMINDER_DUE_BASIS_LEGAL,
            allow_blank=False,
        )
        or ""
    )
    value = raw.strip().lower()
    if value in (ANNUITY_REMINDER_DUE_BASIS_LEGAL, ANNUITY_REMINDER_DUE_BASIS_EFFECTIVE):
        return value
    return ANNUITY_REMINDER_DUE_BASIS_LEGAL


def is_notification_enabled() -> bool:
    """Check if deadline email notifications are enabled in SystemConfig."""
    return _deadline_email_enabled()


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    try:
        text = str(value).strip().lower()
    except Exception:
        return bool(default)
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(default)


def _deadline_email_enabled() -> bool:
    # Priority:
    # 1) DEADLINE_EMAIL_ENABLED (new key)
    # 2) DEADLINE_NOTIFICATION_ENABLED (legacy fallback)
    sentinel = object()
    raw = ConfigService.get_raw("DEADLINE_EMAIL_ENABLED", sentinel, allow_blank=True)
    if raw is sentinel:
        return ConfigService.get_bool("DEADLINE_NOTIFICATION_ENABLED", True)
    return _coerce_bool(raw, ConfigService.get_bool("DEADLINE_NOTIFICATION_ENABLED", True))


def _email_sender_configured() -> bool:
    if not has_app_context():
        # Scheduler/CLI normally has app context. Keep permissive fallback for call safety.
        return True
    sender = current_app.config.get("MAIL_DEFAULT_SENDER")
    smtp_user = current_app.config.get("MAIL_USERNAME")
    if isinstance(sender, (list, tuple)):
        sender = sender[-1] if sender else ""
    sender_text = str(sender or "").strip()
    smtp_user_text = str(smtp_user or "").strip()
    return bool(sender_text or smtp_user_text)


def _sanitize_reminder_days(values: list[int] | None, *, fallback: list[int]) -> list[int]:
    """
    Normalize reminder day list to stable, safe integers.

    - removes duplicates while preserving input order
    - ignores negative/non-integer values
    - caps unrealistic values to avoid accidental huge range scans
    """
    source = list(values or [])
    out: list[int] = []
    seen: set[int] = set()
    for raw in source:
        try:
            days = int(raw)
        except Exception:
            continue
        if days < 0 or days > 3650:
            continue
        if days in seen:
            continue
        seen.add(days)
        out.append(days)
    return out or list(fallback)


# Backward-compatible alias (kept for callers/tests that still import the constant).
DEFAULT_REMINDER_DAYS = list(FALLBACK_REMINDER_DAYS)


@dataclass
class NotificationPayload:
    """Notification payload data."""

    entity_type: str
    entity_id: str
    case_id: str | None
    our_ref: str
    right_name: str
    title: str
    due_date: date
    days_before: int
    recipient_email: str
    recipient_name: str


def is_channel_enabled(channel_name: str) -> bool:
    value = (channel_name or "").strip().lower()
    if value == "email":
        if not _deadline_email_enabled():
            return False
        if not _email_sender_configured():
            warn_key = "deadline_email_sender_missing"
            if warn_key not in _CHANNEL_DISABLED_WARNED:
                _CHANNEL_DISABLED_WARNED.add(warn_key)
                logger.warning(
                    "Deadline email channel disabled: MAIL_DEFAULT_SENDER/MAIL_USERNAME not set"
                )
            return False
        return True
    if value != "email":
        return False
    return True


class NotificationChannel(ABC):
    """Abstract base class for notification channels."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Return channel identifier."""
        pass

    @abstractmethod
    def send(self, payload: NotificationPayload) -> bool:
        """Send notification. Returns True on success."""
        pass


class EmailChannel(NotificationChannel):
    """Email notification channel."""

    @property
    def channel_name(self) -> str:
        return "email"

    def send(self, payload: NotificationPayload) -> bool:
        """Send email notification."""
        self.last_error = None
        try:
            smtp_host = current_app.config.get("MAIL_SERVER", "localhost")
            smtp_port = current_app.config.get("MAIL_PORT", 587)
            smtp_user = current_app.config.get("MAIL_USERNAME")
            smtp_pass = current_app.config.get("MAIL_PASSWORD")
            smtp_tls = current_app.config.get("MAIL_USE_TLS", True)
            sender = current_app.config.get("MAIL_DEFAULT_SENDER", smtp_user)

            if not sender:
                logger.warning("No email sender configured")
                self.last_error = "missing_email_sender"
                return False

            subject = self._build_subject(payload)
            body = self._build_body(payload)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = payload.recipient_email
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if smtp_tls:
                    server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(sender, [payload.recipient_email], msg.as_string())

            logger.info(f"Email sent to {payload.recipient_email}: {subject}")
            return True

        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"Failed to send email: {e}")
            report_swallowed_exception(
                e,
                context="deadline_notifications.email_send",
                log_key="deadline_notifications.email_send",
            )
            return False

    def _build_subject(self, payload: NotificationPayload) -> str:
        return f"[IPM] Deadline Notice - {payload.our_ref} {payload.title} ({payload.days_before} )"

    def _build_body(self, payload: NotificationPayload) -> str:
        return f"""{payload.recipient_name},

Next Deadline {payload.days_before}  Deadline:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 Matter reference: {payload.our_ref}
📌 Title: {payload.right_name}
⏰ Deadline: {payload.title}
📅 Due date: {payload.due_date.isoformat()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

IPM from Confirm.

※   Auto Send.
"""


def _get_weekday_label(d: date) -> str:
    days = ["", "", "", "", "", "", ""]
    return days[d.weekday()]


def _parse_date(v) -> date | None:
    """Parse date string to date object."""
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        try:
            return v.date()
        except Exception:
            return None
    try:
        s = normalize_date_str(v)
        if not s:
            return None
        return date.fromisoformat(s)
    except Exception:
        return None


def _get_user_by_staff_party_id(staff_party_id: str) -> User | None:
    """Get User by staff_party_id."""
    if not staff_party_id:
        return None
    return User.query.filter_by(staff_party_id=staff_party_id, is_active=True).first()


def _fallback_deadline_email_recipient() -> str:
    for key in (
        "DEADLINE_UNASSIGNED_FALLBACK_EMAIL",
        "DEADLINE_UNASSIGNED_FALLBACK_EMAILS",
    ):
        raw = (ConfigService.get_str(key, "", allow_blank=True) or "").strip()
        if not raw:
            continue
        normalized = raw.replace(";", ",")
        for token in normalized.split(","):
            email = token.strip()
            if email:
                return email
    return ""


def _resolve_fallback_user_for_docket(
    docket_item: DocketItem,
    *,
    matter: Matter | None,
    user_cache: dict[str, User] | None = None,
) -> User | None:
    if not docket_item or not matter:
        return None
    try:
        from app.services.deadlines.mgmt_deadlines import _resolve_owner_from_matter_staff

        is_mgmt_task = (getattr(docket_item, "name_ref", "") or "").strip().upper().startswith(
            "MGMT:"
        ) or (getattr(docket_item, "category", "") or "").strip().upper() in (
            "MGMT",
            "SLA",
            "ADMIN",
        )
        category_type = "MGMT" if is_mgmt_task else "WORK"
        staff_id = _resolve_owner_from_matter_staff(
            str(matter.matter_id),
            category_type=category_type,
        )
        staff_id = (staff_id or "").strip()
        if not staff_id:
            return None
        if user_cache is not None:
            cached = user_cache.get(staff_id)
            if cached is not None:
                return cached
        return _get_user_by_staff_party_id(staff_id)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline_notifications.resolve_fallback_user_for_docket",
            log_key="deadline_notifications.resolve_fallback_user_for_docket",
            log_window_seconds=300,
        )
        return None


def _is_notification_sent(
    entity_type: str,
    entity_id: str,
    channel: str,
    days_before: int,
    due_date: date | None = None,
) -> bool:
    """Check if notification was already sent."""
    try:
        query = NotificationLog.query.filter_by(
            entity_type=entity_type,
            entity_id=entity_id,
            channel=channel,
            days_before=days_before,
            status="sent",
        )
        if due_date is not None:
            query = query.filter(NotificationLog.due_date == due_date)
        existing = query.first()
        return existing is not None
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="deadline_notifications._is_notification_sent.rollback",
                log_key="deadline_notifications._is_notification_sent.rollback",
                log_window_seconds=300,
            )
        report_swallowed_exception(
            exc,
            context="deadline_notifications._is_notification_sent",
            log_key="deadline_notifications._is_notification_sent",
            log_window_seconds=300,
        )
        return False


def _log_notification(
    entity_type: str,
    entity_id: str,
    channel: str,
    days_before: int,
    recipient: str,
    due_date: date | None = None,
    status: str = "sent",
    error_message: str | None = None,
) -> None:
    """Log notification to prevent duplicates."""
    try:
        query = NotificationLog.query.filter_by(
            entity_type=entity_type,
            entity_id=entity_id,
            channel=channel,
            days_before=days_before,
        )
        if due_date is not None:
            query = query.filter(NotificationLog.due_date == due_date)
        existing = query.first()
        if existing:
            existing.recipient = recipient
            existing.status = status
            existing.error_message = error_message
            existing.due_date = due_date
            existing.sent_at = datetime.utcnow()
        else:
            log = NotificationLog(
                entity_type=entity_type,
                entity_id=entity_id,
                channel=channel,
                days_before=days_before,
                recipient=recipient,
                due_date=due_date,
                status=status,
                error_message=error_message,
            )
            db.session.add(log)
        db.session.commit()
    except Exception as e:
        logger.warning(f"Failed to log notification: {e}")
        report_swallowed_exception(
            e,
            context="deadline_notifications.log_notification",
            log_key="deadline_notifications.log_notification",
        )
        db.session.rollback()


def _channel_last_error(channel: NotificationChannel) -> str | None:
    raw = getattr(channel, "last_error", None)
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:1000]


def _annuity_legal_due_date(annuity_item: AnnuityItem) -> date | None:
    return _parse_date(annuity_item.due_date) or _parse_date(annuity_item.extended_due_date)


def _annuity_reminder_due_date(annuity_item: AnnuityItem) -> date | None:
    """
    Reminder due-date resolver for annuity notifications.

    Defaults to legal due date to match "Annuity Fee Deadline" expectations.
    Falls back to effective due date only when legal due is absent.
    """
    from app.services.annuity.annuity_policy import effective_due_date_str

    legal_due = _annuity_legal_due_date(annuity_item)
    basis = _annuity_reminder_due_basis()
    if basis == ANNUITY_REMINDER_DUE_BASIS_EFFECTIVE:
        return _parse_date(effective_due_date_str(annuity_item)) or legal_due
    return legal_due or _parse_date(effective_due_date_str(annuity_item))


def get_upcoming_docket_items(
    days_before_list: list[int] = None,
) -> list[tuple[DocketItem, int]]:
    """
    Get upcoming docket items that need notifications.

    Args:
        days_before_list: List of days before deadline to check

    Returns:
        List of (DocketItem, days_before) tuples
    """
    if days_before_list is None:
        days_before_list = get_reminder_days_from_config()
    days_before_list = _sanitize_reminder_days(days_before_list, fallback=FALLBACK_REMINDER_DAYS)

    today = date.today()
    results = []

    target_dates: dict[str, int] = {}
    for days in days_before_list:
        target_date = today + timedelta(days=days)
        target_dates[target_date.isoformat()] = days

    if not target_dates:
        return results

    try:
        from sqlalchemy import func

        due_text = effective_due_text_expr(
            DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
        )
        query = DocketItem.query.filter(
            or_(
                DocketItem.done_date.is_(None),
                DocketItem.done_date == "",
            ),
            due_text.isnot(None),
            due_text.in_(list(target_dates.keys())),
            visible_on_or_before(DocketItem, target_date=today),
        )
        if hasattr(DocketItem, "is_deleted"):
            query = query.filter(
                or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
            )
        items = query.all()
    except Exception:
        due_filters = []
        for target_str in target_dates.keys():
            due_filters.append(
                or_(
                    DocketItem.extended_due_date.like(f"{target_str}%"),
                    and_(
                        (
                            DocketItem.extended_due_date.is_(None)
                            | (DocketItem.extended_due_date == "")
                        ),
                        DocketItem.due_date.like(f"{target_str}%"),
                    ),
                )
            )
        query = DocketItem.query.filter(
            or_(
                DocketItem.done_date.is_(None),
                DocketItem.done_date == "",
            ),
            or_(
                DocketItem.due_date.isnot(None),
                DocketItem.extended_due_date.isnot(None),
            ),
            or_(*due_filters),
            visible_on_or_before(DocketItem, target_date=today),
        )
        if hasattr(DocketItem, "is_deleted"):
            query = query.filter(
                or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
            )
        items = query.all()

    for item in items:
        if not is_visible_by_date(item, today=today):
            continue
        due_date = effective_due_for_work(
            getattr(item, "due_date", None),
            getattr(item, "extended_due_date", None),
        )
        if not due_date:
            continue
        days = target_dates.get(due_date.isoformat())
        if days is None:
            continue
        results.append((item, days))

    return results


def get_upcoming_annuity_items(
    days_before_list: list[int] = None,
) -> list[tuple[AnnuityItem, int]]:
    """Get upcoming annuity items that need notifications."""
    from app.services.annuity.annuity_management import (
        resolve_annuity_management_disabled_matter_ids,
    )

    if days_before_list is None:
        # Use the dedicated annuity reminder schedule when not explicitly provided.
        days_before_list = get_annuity_reminder_days_from_config()
    days_before_list = _sanitize_reminder_days(
        days_before_list, fallback=FALLBACK_ANNUITY_REMINDER_DAYS
    )

    today = date.today()
    results = []

    target_dates: dict[str, int] = {}
    for days in days_before_list:
        target_date = today + timedelta(days=days)
        target_dates[target_date.isoformat()] = days

    if not target_dates:
        return results

    try:
        paid_date_text = func.nullif(func.trim(AnnuityItem.paid_date), "")
        query = AnnuityItem.query.filter(
            or_(
                AnnuityItem.annuity_status.is_(None),
                AnnuityItem.annuity_status.notin_(("paid", "giveup")),
            ),
            paid_date_text.is_(None),
            or_(
                AnnuityItem.due_date.isnot(None),
                AnnuityItem.extended_due_date.isnot(None),
                AnnuityItem.internal_due_date.isnot(None),
            ),
        )
        if hasattr(AnnuityItem, "is_deleted"):
            query = query.filter(
                or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None))
            )
        items = query.all()
    except Exception as exc:
        logger.exception("Failed to query upcoming annuity items")
        report_swallowed_exception(
            exc,
            context="deadline_notifications.get_upcoming_annuity",
            log_key="deadline_notifications.get_upcoming_annuity",
        )
        return []

    from app.services.annuity.annuity_policy import compute_status, is_registration_prepaid_cycle

    matter_map: dict[str, Matter] = {}
    matter_ids = {item.matter_id for item in items if item.matter_id}
    disabled_matter_ids = (
        resolve_annuity_management_disabled_matter_ids(matter_ids) if matter_ids else set()
    )
    if matter_ids:
        matter_map = {
            m.matter_id: m for m in Matter.query.filter(Matter.matter_id.in_(matter_ids)).all()
        }

    for item in items:
        if str(getattr(item, "matter_id", "") or "").strip() in disabled_matter_ids:
            continue
        if compute_status(item, today=today) in ("paid", "giveup"):
            continue
        matter = matter_map.get(item.matter_id)
        if is_registration_prepaid_cycle(item, matter):
            continue
        due_date = _annuity_reminder_due_date(item)
        if not due_date:
            continue
        days = target_dates.get(due_date.isoformat())
        if days is None:
            continue
        results.append((item, days))

    return results


def send_docket_item_notification(
    docket_item: DocketItem,
    days_before: int,
    channel: NotificationChannel,
    *,
    matter_cache: dict[str, Matter] | None = None,
    user_cache: dict[str, User] | None = None,
    log: bool = True,
) -> bool:
    """
    Send notification for a docket item.

    Returns:
        True if sent successfully, False otherwise
    """
    # Skip if done
    if (docket_item.done_date or "").strip():
        return False
    if hasattr(docket_item, "is_deleted") and bool(getattr(docket_item, "is_deleted", False)):
        return False
    if not is_visible_by_date(docket_item):
        return False

    # Get due date
    due_date = effective_due_for_work(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    if not due_date:
        return False

    # Skip if already sent (same entity + channel + D-n + due-date snapshot).
    dedupe_days_before = int(days_before)
    if _is_notification_sent(
        "docket_item",
        docket_item.docket_id,
        channel.channel_name,
        dedupe_days_before,
        due_date,
    ):
        logger.debug(
            "Notification already sent for docket %s %sd (due=%s)",
            docket_item.docket_id,
            days_before,
            due_date.isoformat(),
        )
        return False

    # Get matter info
    if matter_cache is not None:
        matter = matter_cache.get(docket_item.matter_id)
    else:
        matter = Matter.query.get(docket_item.matter_id)
    our_ref = (matter.our_ref if matter else "") or ""
    right_name = (matter.right_name if matter else "") or ""

    # Get recipient
    owner_staff_party_id = str(getattr(docket_item, "owner_staff_party_id", "") or "").strip()
    configured_fallback_email = _fallback_deadline_email_recipient()
    use_configured_fallback_for_unassigned = (
        channel.channel_name == "email"
        and not owner_staff_party_id
        and bool(configured_fallback_email)
    )

    if user_cache is not None:
        user = user_cache.get(owner_staff_party_id)
    else:
        user = _get_user_by_staff_party_id(owner_staff_party_id)
    if (not use_configured_fallback_for_unassigned) and (not user or not user.email) and matter:
        user = _resolve_fallback_user_for_docket(
            docket_item=docket_item,
            matter=matter,
            user_cache=user_cache,
        )

    recipient_email = (getattr(user, "email", "") or "").strip()
    recipient_name = (
        (getattr(user, "display_name", "") or "").strip()
        or (getattr(user, "username", "") or "").strip()
        or "Unassigned"
    )

    if use_configured_fallback_for_unassigned:
        recipient_email = configured_fallback_email
        recipient_name = "Unassigned (Fallback)"
    elif not recipient_email:
        fallback_email = configured_fallback_email or _fallback_deadline_email_recipient()
        if fallback_email:
            recipient_email = fallback_email
            recipient_name = "Unassigned (Fallback)"
        else:
            logger.debug("No recipient for docket %s", docket_item.docket_id)
            return False

    # Build payload
    payload = NotificationPayload(
        entity_type="docket_item",
        entity_id=docket_item.docket_id,
        case_id=str(docket_item.matter_id or "").strip() or None,
        our_ref=our_ref,
        right_name=right_name,
        title=(docket_item.name_free or docket_item.name_ref or "").strip(),
        due_date=due_date,
        days_before=days_before,
        recipient_email=recipient_email,
        recipient_name=recipient_name,
    )

    # Send
    success = channel.send(payload)
    error_message = _channel_last_error(channel) if not success else None

    # Log
    if log:
        _log_notification(
            entity_type="docket_item",
            entity_id=docket_item.docket_id,
            channel=channel.channel_name,
            days_before=dedupe_days_before,
            recipient=recipient_email,
            due_date=due_date,
            status="sent" if success else "failed",
            error_message=error_message,
        )

    return success


def send_annuity_item_notification(
    annuity_item: AnnuityItem,
    days_before: int,
    channel: NotificationChannel,
    *,
    log: bool = True,
) -> bool:
    """Send notification for an annuity item."""
    from app.services.annuity.annuity_management import is_annuity_management_disabled_for_matter
    from app.services.annuity.annuity_policy import compute_status, is_registration_prepaid_cycle

    # Skip if paid/giveup
    if compute_status(annuity_item, today=date.today()) in ("paid", "giveup"):
        return False
    if hasattr(annuity_item, "is_deleted") and bool(getattr(annuity_item, "is_deleted", False)):
        return False
    if is_annuity_management_disabled_for_matter(getattr(annuity_item, "matter_id", None)):
        return False

    due_date = _annuity_reminder_due_date(annuity_item)
    if not due_date:
        return False

    # Get matter info
    matter = Matter.query.get(annuity_item.matter_id)
    if is_registration_prepaid_cycle(annuity_item, matter):
        return False

    # Skip if already sent (same entity + channel + D-n + due-date snapshot).
    dedupe_days_before = int(days_before)
    if _is_notification_sent(
        "annuity_item",
        annuity_item.annuity_id,
        channel.channel_name,
        dedupe_days_before,
        due_date,
    ):
        return False

    our_ref = (matter.our_ref if matter else "") or ""
    right_name = (matter.right_name if matter else "") or ""

    # Prefer annuity owner, fallback to case manager/handler
    user = _get_user_by_staff_party_id(annuity_item.owner_staff_party_id)
    if (not user or not user.email) and matter:
        from app.services.deadlines.mgmt_deadlines import (
            AssigneeResolver,
            _merge_custom_fields,
            _resolve_assignee_value,
        )

        custom_data = _merge_custom_fields(matter.matter_id)
        resolver = AssigneeResolver()
        staff_id = resolver.resolve(
            _resolve_assignee_value(custom_data, "manager")
            or _resolve_assignee_value(custom_data, "handler")
        )
        if staff_id:
            user = _get_user_by_staff_party_id(staff_id)

    if not user or not user.email:
        logger.warning(
            "Skip annuity notification: missing recipient (annuity_id=%s, matter_id=%s)",
            getattr(annuity_item, "annuity_id", None),
            getattr(annuity_item, "matter_id", None),
        )
        if log:
            _log_notification(
                entity_type="annuity_item",
                entity_id=annuity_item.annuity_id,
                channel=channel.channel_name,
                days_before=dedupe_days_before,
                recipient="",
                due_date=due_date,
                status="failed",
                error_message="missing_recipient",
            )
        return False

    facts = MatterFacts.query.get(annuity_item.matter_id)
    right_type = normalize_renewal_right_type(
        getattr(facts, "right_type_norm", None),
        getattr(matter, "matter_type", None),
        getattr(matter, "right_group", None),
        getattr(matter, "our_ref", None),
    )
    jurisdiction = normalize_renewal_jurisdiction(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
        getattr(matter, "our_ref", None),
    )

    payload = NotificationPayload(
        entity_type="annuity_item",
        entity_id=annuity_item.annuity_id,
        case_id=str(annuity_item.matter_id or "").strip() or None,
        our_ref=our_ref,
        right_name=right_name,
        title=renewal_workflow_name(
            annuity_item.cycle_no,
            right_type=right_type,
            jurisdiction=jurisdiction,
        ),
        due_date=due_date,
        days_before=days_before,
        recipient_email=(getattr(user, "email", "") or "").strip(),
        recipient_name=(
            (getattr(user, "display_name", "") or "").strip()
            or (getattr(user, "username", "") or "").strip()
            or "Unassigned"
        ),
    )

    success = channel.send(payload)
    error_message = _channel_last_error(channel) if not success else None

    if log:
        _log_notification(
            entity_type="annuity_item",
            entity_id=annuity_item.annuity_id,
            channel=channel.channel_name,
            days_before=dedupe_days_before,
            recipient=(getattr(user, "email", "") or "").strip(),
            due_date=due_date,
            status="sent" if success else "failed",
            error_message=error_message,
        )

    return success


def send_all_deadline_notifications(
    channel: NotificationChannel | None = None,
    days_before_list: list[int] | None = None,
    *,
    log: bool = True,
) -> tuple[int, int]:
    """
    Send all pending deadline notifications.

    Args:
        channel: Notification channel (default: EmailChannel)
        days_before_list: Days before deadline to check

    Returns:
        Tuple of (sent_count, failed_count)
    """
    if channel is None:
        channel = EmailChannel()

    if not is_channel_enabled(channel.channel_name):
        logger.info("Deadline notifications skipped: channel disabled (%s)", channel.channel_name)
        return 0, 0

    sent = 0
    failed = 0

    # Process docket items
    docket_items = get_upcoming_docket_items(days_before_list)
    matter_cache: dict[str, Matter] = {}
    user_cache: dict[str, User] = {}
    if docket_items:
        matter_ids = {item.matter_id for item, _ in docket_items if item.matter_id}
        staff_ids = {
            item.owner_staff_party_id for item, _ in docket_items if item.owner_staff_party_id
        }
        if matter_ids:
            matter_cache = {
                m.matter_id: m for m in Matter.query.filter(Matter.matter_id.in_(matter_ids)).all()
            }
        if staff_ids:
            user_cache = {
                u.staff_party_id: u
                for u in User.query.filter(
                    User.staff_party_id.in_(staff_ids),
                    User.is_active.is_(True),
                ).all()
            }

    for item, days in docket_items:
        try:
            if send_docket_item_notification(
                item,
                days,
                channel,
                matter_cache=matter_cache,
                user_cache=user_cache,
                log=log,
            ):
                sent += 1
        except Exception as e:
            logger.error(f"Failed to notify docket {item.docket_id}: {e}")
            failed += 1

    for item, days in get_upcoming_annuity_items(days_before_list):
        try:
            if send_annuity_item_notification(item, days, channel, log=log):
                sent += 1
        except Exception as e:
            logger.error(f"Failed to notify annuity {item.annuity_id}: {e}")
            failed += 1
    logger.info(f"Notification summary: {sent} sent, {failed} failed")
    return sent, failed


def retry_failed_deadline_notifications(
    channel: NotificationChannel | None = None,
    *,
    lookback_days: int = 14,
    batch_size: int = 200,
) -> tuple[int, int]:
    """
    Retry recently failed deadline notifications.

    This complements the daily scheduler run and recovers transient provider failures
    (SMTP downtime, network blips) without waiting for the next D-n window.
    """
    if channel is None:
        channel = EmailChannel()

    if not is_channel_enabled(channel.channel_name):
        logger.info(
            "Failed-deadline notification retry skipped: channel disabled (%s)",
            channel.channel_name,
        )
        return 0, 0

    lookback_days = max(1, int(lookback_days or 14))
    batch_size = max(1, min(2000, int(batch_size or 200)))

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    rows = (
        NotificationLog.query.filter(
            NotificationLog.status == "failed",
            NotificationLog.channel == channel.channel_name,
            NotificationLog.sent_at >= cutoff,
        )
        .order_by(NotificationLog.sent_at.asc())
        .limit(batch_size)
        .all()
    )

    sent = 0
    failed = 0
    for row in rows:
        try:
            days_before = int(row.days_before or 0)
            if row.entity_type == "docket_item":
                item = DocketItem.query.get(row.entity_id)
                if item and send_docket_item_notification(item, days_before, channel):
                    sent += 1
                else:
                    failed += 1
            elif row.entity_type == "annuity_item":
                item = AnnuityItem.query.get(row.entity_id)
                if item is not None:
                    from app.services.annuity.annuity_management import (
                        is_annuity_management_disabled_for_matter,
                    )

                    if is_annuity_management_disabled_for_matter(getattr(item, "matter_id", None)):
                        try:
                            row.status = "sent"
                            row.error_message = "skipped_annuity_management_disabled"
                            db.session.add(row)
                            db.session.commit()
                        except Exception as exc:
                            try:
                                db.session.rollback()
                            except Exception as rollback_exc:
                                report_swallowed_exception(
                                    rollback_exc,
                                    context="deadline_notifications.retry_failed.disabled.rollback",
                                    log_key="deadline_notifications.retry_failed.disabled.rollback",
                                    log_window_seconds=300,
                                )
                            report_swallowed_exception(
                                exc,
                                context="deadline_notifications.retry_failed.disabled.mark_skipped",
                                log_key="deadline_notifications.retry_failed.disabled.mark_skipped",
                                log_window_seconds=300,
                            )
                        continue
                if item and send_annuity_item_notification(item, days_before, channel):
                    sent += 1
                else:
                    failed += 1
            else:
                failed += 1
        except Exception as exc:
            logger.error(
                "Failed retry for notification_log id=%s: %s", getattr(row, "id", None), exc
            )
            failed += 1

    logger.info(
        "Failed-deadline notification retry summary (%s): %s sent, %s failed",
        channel.channel_name,
        sent,
        failed,
    )
    return sent, failed
