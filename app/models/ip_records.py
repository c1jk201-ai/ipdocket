from .annuity import AnnuityItem
from .assets import FileAsset, MatterFileAsset
from .billing_guardrail import BillingGuardrailFinding
from .cited_reference import CitedReference
from .communication import (
    Communication,
    CommunicationFileAsset,
    OfficeAction,
    OfficeActionFileAsset,
)
from .docket import DocketItem
from .document_search import DocumentSearchIndex
from .email_automation import (
    AutomationChangeSet,
    AutomationChangeSnapshot,
    AutomationFieldFeedback,
    AutomationReviewFeedback,
    EmailAttachment,
    EmailIngestionLog,
    EmailMessage,
    EmailMessageMatterLink,
    EmailMessageTombstone,
    ExtractionResult,
    FieldEvidence,
    IngestionRun,
    MailMatchCandidate,
    MatterMatch,
)
from .matter import (
    EventKeyMap,
    Family,
    Matter,
    MatterCustomField,
    MatterEvent,
    MatterFamily,
    MatterIdentifier,
    MatterMemo,
    MatterMemoFileAsset,
    MatterPartyRole,
    MatterProgress,
    MatterStaffAssignment,
    MatterStatusHistory,
    VMatterOverview,
)
from .matter_status_recalc_queue import MatterStatusRecalcQueue
from .legacy_finance import (
    CaseExpenseInvoiceMap,
    ExternalInvoiceCaseLink,
    ExternalInvoiceCaseMap,
    LegacyExpense,
    LegacyExpensePayment,
    LegacyInvoice,
    LegacyInvoicePayment,
)
from .raw_import import RawImportField
from .risk_control import DeadlineReviewQueue, MatterRiskFact

# UI / Automation review helpers (desktop UX)
from .ui_prefs import AutomationReviewTemplate, UserUiPreference  # noqa: F401
from .workflow_playbook import WorkflowPlaybookTemplate
