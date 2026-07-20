"""Data Transfer Objects for upload operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DuplicateInfo:
    """Information about a duplicate file."""

    upload_name: str
    original_name: str
    created_at: str | None
    file_asset_id: str


@dataclass
class UploadBatch:
    """Batch of staged files for processing."""

    staged_file_ids: list[str]
    mode: str  # 'new' or 'legacy'
    form_data: dict = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """Result of upload analysis."""

    needs_param_confirm: bool = False
    needs_duplicate_confirm: bool = False
    auto_apply: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    duplicates: list[DuplicateInfo] = field(default_factory=list)
    render_target: str = "single"  # 'single', 'batch', 'confirm'
    context: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    """Result of applying upload changes."""

    success: bool
    matter_id: str | None = None
    status_changed: bool = False
    workflow_created: bool = False
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ParsedBibResult:
    """Result of parsing a BIB file."""

    matter_id: str
    our_ref: str
    doc_type: str
    staged_file_id: str
    auto_apply: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    filename: str = ""
