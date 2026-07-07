from dataclasses import dataclass
from typing import Any, List, Mapping, Optional


@dataclass(frozen=True)
class DomesticPatentUpdateCommand:
    matter_id: str
    division: str
    case_type: str
    form_data: Mapping[str, Any]  # Request form data
    actor_user_id: Optional[int] = None


@dataclass
class DomesticPatentUpdateResult:
    updated: bool
    warnings: List[str]
    dockets_touched: int


@dataclass(frozen=True)
class MatterCreatePrepareCommand:
    division: str
    case_type: str
    raw_args: Mapping[str, str]


@dataclass
class MatterCreatePrepareResult:
    division: str
    case_type: str
    field_layout: List[Any]
    field_meta: Mapping[str, Any]
    idempotency_key: str
    prefill: Mapping[str, Any]
    context: Mapping[str, Any]  # staff logic, etc.


@dataclass(frozen=True)
class MatterCreateCommand:
    division: str
    case_type: str
    form_data: Mapping[str, Any]
    files: Mapping[str, Any]
    actor_user_id: int
    idempotency_key: str


@dataclass
class MatterCreateResult:
    success: bool
    matter_id: Optional[str] = None
    existing_id: Optional[str] = None  # for idempotency used
    error: Optional[str] = None
    validation_errors: Optional[List[Any]] = None
    redirect_to_list: bool = False  # for idempotency return
