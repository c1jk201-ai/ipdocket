from __future__ import annotations

from dataclasses import dataclass

from flask import has_app_context

from app.services.matter.status_normalization import _get_json_config

_RED_RULES_CONFIG_KEY = "AUTO_STATUS_RED_RULES_JSON"


@dataclass(frozen=True)
class RedRule:
    key: str
    label: str
    deadline_event_key: str
    completion_event_key: str
    activation_event_keys: tuple[str, ...]
    red_class: str
    stage: int


_DEFAULT_RED_RULES: tuple[RedRule, ...] = (
    RedRule(
        key="APPLICATION_DEADLINE",
        label="FilingDeadline",
        deadline_event_key="APPLICATION_DEADLINE",
        completion_event_key="APPLICATION_DATE",
        activation_event_keys=(),
        red_class="pipeline",
        stage=10,
    ),
    RedRule(
        key="FOREIGN_FILING_DEADLINE",
        label="ForeignFilingDeadline",
        deadline_event_key="FOREIGN_FILING_DEADLINE",
        completion_event_key="FOREIGN_FILING_DATE",
        activation_event_keys=("APPLICATION_DATE",),
        red_class="pipeline",
        stage=20,
    ),
    RedRule(
        key="EXAM_REQUEST_DEADLINE",
        label="Examination requestDeadline",
        deadline_event_key="EXAM_REQUEST_DEADLINE",
        completion_event_key="EXAM_REQUESTED",
        activation_event_keys=("APPLICATION_DATE",),
        red_class="pipeline",
        stage=30,
    ),
    RedRule(
        key="APPEAL_DEADLINE",
        label="Deadline",
        deadline_event_key="APPEAL_DEADLINE",
        completion_event_key="",
        activation_event_keys=("REJECTION_RECEIVED_DATE",),
        red_class="interrupt",
        stage=35,
    ),
    RedRule(
        key="REGISTRATION_DEADLINE",
        label="RegistrationDeadline",
        deadline_event_key="REGISTRATION_DEADLINE",
        completion_event_key="REGISTRATION_DATE",
        activation_event_keys=(),
        red_class="interrupt",
        stage=40,
    ),
    RedRule(
        key="PENALTY_REG_DEADLINE",
        label="RegistrationDeadline",
        deadline_event_key="PENALTY_REG_DEADLINE",
        completion_event_key="REGISTRATION_DATE",
        activation_event_keys=(),
        red_class="interrupt",
        stage=45,
    ),
)


def _coerce_activation_keys(raw: object | None) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(part).strip() for part in raw if str(part).strip())
    return ()


def _parse_red_rules(raw: object | None) -> list[RedRule]:
    if not isinstance(raw, list):
        return []
    parsed: list[RedRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = (item.get("key") or "").strip()
        label = (item.get("label") or "").strip()
        deadline_event_key = (item.get("deadline_event_key") or "").strip()
        completion_event_key = (item.get("completion_event_key") or "").strip()
        red_class = (item.get("red_class") or "").strip().lower() or "pipeline"
        try:
            stage = int(item.get("stage") or 0)
        except (TypeError, ValueError):
            stage = 0
        activation_event_keys = _coerce_activation_keys(item.get("activation_event_keys"))
        if not key or not label or not deadline_event_key:
            continue
        if red_class not in ("interrupt", "pipeline"):
            continue
        parsed.append(
            RedRule(
                key=key,
                label=label,
                deadline_event_key=deadline_event_key,
                completion_event_key=completion_event_key,
                activation_event_keys=activation_event_keys,
                red_class=red_class,
                stage=stage,
            )
        )
    return parsed


def _get_red_rules() -> tuple[RedRule, ...]:
    if not has_app_context():
        return _DEFAULT_RED_RULES
    rules = _parse_red_rules(_get_json_config(_RED_RULES_CONFIG_KEY))
    return tuple(rules) if rules else _DEFAULT_RED_RULES


def _get_red_rule_by_key() -> dict[str, RedRule]:
    return {rule.key: rule for rule in _get_red_rules()}


def _get_red_rule_by_label() -> dict[str, RedRule]:
    return {rule.label: rule for rule in _get_red_rules()}
