from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.core.config_service import ConfigService


@dataclass(frozen=True)
class ExtractionValidation:
    level: str
    warnings: list[str] = field(default_factory=list)


def _automation_level_cap() -> str:
    override = ConfigService.get_raw(
        "FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE",
        "",
        allow_blank=True,
    )
    if str(override or "").strip():
        return str(override).strip().upper()
    level = ConfigService.get_str(
        "FOREIGN_EMAIL_AUTOMATION_LEVEL",
        "AUTO_DRAFT",
        allow_blank=True,
    )
    return (level or "AUTO_DRAFT").strip().upper() or "AUTO_DRAFT"


def validate_extraction(
    structured_json: dict[str, Any] | None,
    *,
    has_matter_match: bool = False,
) -> ExtractionValidation:
    payload = structured_json if isinstance(structured_json, dict) else {}
    warnings = [str(item) for item in (payload.get("warnings") or []) if item is not None]
    evidence_map = payload.get("evidence_map")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    dockets = payload.get("dockets") if isinstance(payload.get("dockets"), list) else []

    if not has_matter_match:
        warnings.append("Matter match missing")
    if dockets and not evidence_map:
        warnings.append("missing evidence")

    level = _automation_level_cap()
    if not has_matter_match:
        level = "HUMAN_REQUIRED"
    elif dockets and not evidence_map:
        level = "AUTO_DRAFT"
    elif not params and not dockets:
        level = "AUTO_DRAFT"
    return ExtractionValidation(level=level, warnings=warnings)
