from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable

from app.extensions import db
from app.models.ip_records import Matter
from app.services.case.case_kind import resolve_profile_case_kind
from app.services.core.config_service import ConfigService
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)

CASE_OUR_REF_NUMBERING_CONFIG_KEY = "CASE_OUR_REF_NUMBERING_JSON"
SPECIAL_OUR_REF_TYPES = frozenset({"PCT", "MADRID", "HAGUE", "COPYRIGHT", "LITIGATION", "MISC"})

_DEFAULT_SEQUENCE_WIDTH = 4
_COUNTRY_WILDCARD_RE = r"[A-Z0-9]{1,8}"
_BRACED_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([^}]+))?\}")
_BARE_DATE_TOKEN_RE = re.compile(r"YYYY|YY|MM|DD", re.IGNORECASE)
_SEQ_FORMAT_RE = re.compile(r"^(?:0+\d*|0?\d{1,2}d|d)$")
_ALLOWED_BRACED_TOKENS = {"code", "country", "seq", "yyyy", "yy", "mm", "dd"}

_US_DEFAULT_CODE_MAP = {
    ("DOM", "PATENT"): "P",
    ("DOM", "UTILITY"): "U",
    ("DOM", "DESIGN"): "D",
    ("DOM", "TRADEMARK"): "T",
    ("INC", "PATENT"): "IP",
    ("INC", "UTILITY"): "IU",
    ("INC", "DESIGN"): "ID",
    ("INC", "TRADEMARK"): "IT",
    ("OUT", "PATENT"): "FP",
    ("OUT", "UTILITY"): "FU",
    ("OUT", "DESIGN"): "FD",
    ("OUT", "TRADEMARK"): "FT",
}

_DEFAULT_NUMBERING_CONFIG_EXAMPLE = {
    "template": "{country}{code}YY{seq:0000}",
    "codes": {
        "DOM:PATENT": "P",
        "DOM:UTILITY": "U",
        "DOM:DESIGN": "D",
        "DOM:TRADEMARK": "T",
        "INC:PATENT": "IP",
        "INC:UTILITY": "IU",
        "INC:DESIGN": "ID",
        "INC:TRADEMARK": "IT",
        "OUT:PATENT": "FP",
        "OUT:UTILITY": "FU",
        "OUT:DESIGN": "FD",
        "OUT:TRADEMARK": "FT",
        "PCT": "PCT",
        "MADRID": "MAD",
        "HAGUE": "HAG",
        "LITIGATION": "L",
        "COPYRIGHT": "CR",
        "MISC": "M",
    },
    "rules": {
        "PCT": {"template": "{code}YY{seq:0000}", "code": "PCT", "country": "PCT"},
        "MADRID": {"template": "{code}YY{seq:0000}", "code": "MAD", "counter_scope": "prefix"},
        "HAGUE": {"template": "{code}YY{seq:0000}", "code": "HAG", "counter_scope": "prefix"},
        "COPYRIGHT": {"template": "{code}YY{seq:0000}", "code": "CR", "counter_scope": "prefix"},
        "LITIGATION": {"template": "{code}YY{seq:0000}", "code": "L", "counter_scope": "prefix"},
        "MISC": {"template": "{code}YY{seq:0000}", "code": "M", "counter_scope": "prefix"},
    },
}


@dataclass
class NextOurRefError(Exception):
    code: str
    message: str
    status: int = 400


@dataclass(frozen=True)
class _OurRefScheme:
    prefix: str
    pattern: re.Pattern
    counter_key: str
    build: Callable[[int], str]
    sequence_pattern: re.Pattern | None = None


@dataclass(frozen=True)
class _DefaultOurRefRule:
    code: str
    country: str
    template: str
    counter_scope: str = "country"
    sequence_country_wildcard: bool = False


@dataclass(frozen=True)
class _TemplatePart:
    kind: str
    value: str
    format_spec: str | None = None


def default_our_ref_numbering_config_json() -> str:
    return json.dumps(_DEFAULT_NUMBERING_CONFIG_EXAMPLE, ensure_ascii=False, indent=2)


def _compute_max_our_ref_seq_from_refs(refs: Iterable[str | None], pattern: re.Pattern) -> int:
    max_num = 0
    for ref in refs:
        if not ref:
            continue
        m = pattern.match(ref.strip())
        if not m:
            continue
        try:
            max_num = max(max_num, int(m.group("num")))
        except Exception:
            continue
    return max_num


def _compute_max_our_ref_seq(prefix: str, pattern: re.Pattern) -> int:
    query = Matter.query.with_entities(Matter.our_ref)
    if prefix:
        query = query.filter(Matter.our_ref.like(f"{prefix}%"))
    refs = query.all()
    return _compute_max_our_ref_seq_from_refs((ref for (ref,) in refs), pattern)


def _reserve_next_seq(*, key: str, max_seq: int, exists_fn) -> int | None:
    """
    Reserve a monotonically increasing sequence in system_config.
    Uses CAS-style UPDATE to avoid relying on SELECT ... FOR UPDATE (portable across DBs).
    NOTE: Caller must COMMIT for reservation to persist.
    """
    try:
        db.session.execute(
            text(
                """
                INSERT INTO system_config (key, value)
                VALUES (:key, :value)
                ON CONFLICT (key) DO NOTHING
                """
            ),
            {"key": key, "value": str(max_seq)},
        )

        for _ in range(6):
            row = db.session.execute(
                text("SELECT value FROM system_config WHERE key = :key"),
                {"key": key},
            ).scalar()

            try:
                current = int(row or 0)
            except Exception:
                current = 0

            if max_seq > current:
                current = max_seq

            next_seq = current + 1
            while exists_fn(next_seq):
                next_seq += 1

            updated = db.session.execute(
                text(
                    """
                    UPDATE system_config
                    SET value = :value
                    WHERE key = :key AND value = :current
                    """
                ),
                {
                    "value": str(next_seq),
                    "key": key,
                    "current": str(row or 0),
                },
            )
            if (updated.rowcount or 0) > 0:
                return next_seq

        return None
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            logger.warning("db.session.rollback failed in _reserve_next_seq", exc_info=True)
        return None


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _clean_code(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw if re.fullmatch(r"[A-Z0-9]{1,12}", raw) else ""


def _clean_country(value: object, default: str = "") -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return default
    return raw if re.fullmatch(r"[A-Z0-9]{1,8}", raw) else default


def _normalize_counter_scope(value: object, default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in {"prefix", "country"} else default


def _literal_parts_with_bare_date_tokens(literal: str) -> list[_TemplatePart]:
    parts: list[_TemplatePart] = []
    cursor = 0
    for match in _BARE_DATE_TOKEN_RE.finditer(literal):
        if match.start() > cursor:
            parts.append(_TemplatePart("literal", literal[cursor : match.start()]))
        parts.append(_TemplatePart("token", match.group(0).lower()))
        cursor = match.end()
    if cursor < len(literal):
        parts.append(_TemplatePart("literal", literal[cursor:]))
    return parts


def _parse_ref_template(template: str) -> list[_TemplatePart]:
    raw = str(template or "").strip()
    if not raw:
        raise ValueError("template is required")

    parts: list[_TemplatePart] = []
    cursor = 0
    seq_count = 0
    for match in _BRACED_TOKEN_RE.finditer(raw):
        literal = raw[cursor : match.start()]
        if "{" in literal or "}" in literal:
            raise ValueError("template contains an invalid brace")
        if literal:
            parts.extend(_literal_parts_with_bare_date_tokens(literal))

        token = match.group(1).strip().lower()
        format_spec = (match.group(2) or "").strip() or None
        if token not in _ALLOWED_BRACED_TOKENS:
            raise ValueError(f"unknown template token: {token}")
        if token == "seq":
            seq_count += 1
            if format_spec and not _SEQ_FORMAT_RE.fullmatch(format_spec):
                raise ValueError("seq format must look like 0000, d, 4d, or 04d")
        elif format_spec:
            raise ValueError(f"{token} token does not support a format spec")
        parts.append(_TemplatePart("token", token, format_spec))
        cursor = match.end()

    tail = raw[cursor:]
    if "{" in tail or "}" in tail:
        raise ValueError("template contains an invalid brace")
    if tail:
        parts.extend(_literal_parts_with_bare_date_tokens(tail))
    if seq_count != 1:
        raise ValueError("template must contain exactly one {seq} token")
    return parts


def _format_seq(seq: int, format_spec: str | None) -> str:
    seq = int(seq)
    if not format_spec:
        return str(seq).zfill(_DEFAULT_SEQUENCE_WIDTH)
    if set(format_spec) == {"0"}:
        return str(seq).zfill(len(format_spec))
    return ("{0:" + format_spec + "}").format(seq)


def _token_value(token: str, values: dict[str, str]) -> str:
    if token == "seq":
        raise ValueError("seq must be handled separately")
    return str(values.get(token, "") or "")


def _render_template(parts: list[_TemplatePart], values: dict[str, str], seq: int) -> str:
    rendered: list[str] = []
    for part in parts:
        if part.kind == "literal":
            rendered.append(part.value)
            continue
        if part.value == "seq":
            rendered.append(_format_seq(seq, part.format_spec))
        else:
            rendered.append(_token_value(part.value, values))
    return "".join(rendered).upper()


def _compile_template_pattern(
    parts: list[_TemplatePart],
    values: dict[str, str],
    *,
    wildcard_country: bool,
) -> tuple[str, re.Pattern]:
    regex_parts: list[str] = []
    prefix_parts: list[str] = []
    before_seq = True
    prefix_locked = False

    for part in parts:
        if part.kind == "literal":
            regex_parts.append(re.escape(part.value))
            if before_seq and not prefix_locked:
                prefix_parts.append(part.value)
            continue

        token = part.value
        if token == "seq":
            regex_parts.append(r"(?P<num>\d+)")
            before_seq = False
            continue

        if token == "country" and wildcard_country:
            regex_parts.append(_COUNTRY_WILDCARD_RE)
            if before_seq:
                prefix_locked = True
            continue

        rendered = _token_value(token, values)
        regex_parts.append(re.escape(rendered))
        if before_seq and not prefix_locked:
            prefix_parts.append(rendered)

    prefix = "".join(prefix_parts).upper()
    return prefix, re.compile("^" + "".join(regex_parts) + "$", re.IGNORECASE)


def _build_template_our_ref_scheme(
    *,
    template: str,
    code: str,
    country: str,
    counter_scope: str,
    sequence_country_wildcard: bool,
) -> _OurRefScheme:
    today = date.today()
    values = {
        "yyyy": f"{today.year:04d}",
        "yy": f"{today.year % 100:02d}",
        "mm": f"{today.month:02d}",
        "dd": f"{today.day:02d}",
        "code": code,
        "country": country,
    }
    parts = _parse_ref_template(template)
    exact_prefix, exact_pattern = _compile_template_pattern(
        parts,
        values,
        wildcard_country=False,
    )
    sequence_prefix, sequence_pattern = _compile_template_pattern(
        parts,
        values,
        wildcard_country=sequence_country_wildcard,
    )
    prefix = sequence_prefix or exact_prefix
    counter_key = f"our_ref_counter:{prefix or 'global'}"
    if counter_scope == "country" and country:
        counter_key = f"{counter_key}:{country}"
    return _OurRefScheme(
        prefix=prefix,
        pattern=exact_pattern,
        counter_key=counter_key,
        build=lambda seq: _render_template(parts, values, seq),
        sequence_pattern=sequence_pattern if sequence_country_wildcard else None,
    )


def _normal_rule_config(div: str, typ: str, country: str) -> _DefaultOurRefRule:
    div, typ = resolve_profile_case_kind(div, typ)
    code = _US_DEFAULT_CODE_MAP.get((div, typ))
    if not code:
        raise NextOurRefError(
            code="unsupported",
            message="Unsupported matter division/type.",
            status=400,
        )
    return _DefaultOurRefRule(
        code=code,
        country=country or "US",
        template="{country}{code}YY{seq:0000}",
        counter_scope="country",
        sequence_country_wildcard=False,
    )


def _default_rule_config(div: str, typ: str, country: str) -> tuple[str, str, _DefaultOurRefRule]:
    if typ == "PCT":
        return (
            div,
            typ,
            _DefaultOurRefRule(
                code="PCT",
                country="PCT",
                template="{code}YY{seq:0000}",
                counter_scope="country",
            ),
        )

    if typ == "MADRID":
        return (
            div,
            typ,
            _DefaultOurRefRule(
                code="MAD",
                country="",
                template="{code}YY{seq:0000}",
                counter_scope="prefix",
            ),
        )

    if typ == "HAGUE":
        return (
            div,
            typ,
            _DefaultOurRefRule(
                code="HAG",
                country="",
                template="{code}YY{seq:0000}",
                counter_scope="prefix",
            ),
        )

    if typ == "LITIGATION":
        return (
            div,
            typ,
            _DefaultOurRefRule(
                code="L",
                country="",
                template="{code}YY{seq:0000}",
                counter_scope="prefix",
            ),
        )

    if typ == "COPYRIGHT":
        return (
            div,
            typ,
            _DefaultOurRefRule(
                code="CR",
                country="",
                template="{code}YY{seq:0000}",
                counter_scope="prefix",
            ),
        )

    if typ == "MISC":
        return (
            div,
            typ,
            _DefaultOurRefRule(
                code="M",
                country="",
                template="{code}YY{seq:0000}",
                counter_scope="prefix",
            ),
        )

    resolved_div, resolved_typ = resolve_profile_case_kind(div, typ)
    return resolved_div, resolved_typ, _normal_rule_config(resolved_div, resolved_typ, country)


def _load_numbering_config() -> dict[str, Any]:
    try:
        config = ConfigService.get_json(CASE_OUR_REF_NUMBERING_CONFIG_KEY, None)
    except Exception:
        logger.warning("Failed to load Our Ref numbering config", exc_info=True)
        return {}
    return config if isinstance(config, dict) else {}


def _rule_candidates(div: str, typ: str) -> list[str]:
    candidates = []
    if div and typ:
        candidates.append(f"{div}:{typ}")
    if typ:
        candidates.append(typ)
    candidates.append("*")
    return candidates


def _select_rule_config(config: dict[str, Any], div: str, typ: str) -> dict[str, Any]:
    raw_rules = config.get("rules")
    if not isinstance(raw_rules, dict):
        return {}
    for key in _rule_candidates(div, typ):
        raw_rule = raw_rules.get(key)
        if isinstance(raw_rule, dict):
            return raw_rule
    return {}


def _configured_code(
    *, config: dict[str, Any], rule_config: dict[str, Any], div: str, typ: str, default: str
) -> str:
    code = _clean_code(rule_config.get("code"))
    if code:
        return code
    raw_codes = config.get("codes")
    if isinstance(raw_codes, dict):
        for key in _rule_candidates(div, typ):
            code = _clean_code(raw_codes.get(key))
            if code:
                return code
    return default


def _build_our_ref_scheme(
    *, division: str, matter_type: str, country: str | None = None
) -> _OurRefScheme:
    div = (division or "").strip().upper()
    typ = (matter_type or "").strip().upper()
    ctry = _clean_country(country)
    div, typ, default_rule = _default_rule_config(div, typ, ctry)

    numbering_config = _load_numbering_config()
    rule_config = _select_rule_config(numbering_config, div, typ)
    code = _configured_code(
        config=numbering_config,
        rule_config=rule_config,
        div=div,
        typ=typ,
        default=default_rule.code,
    )
    country_value = ctry or _clean_country(rule_config.get("country"), default_rule.country)
    if typ in SPECIAL_OUR_REF_TYPES:
        template = str(rule_config.get("template") or default_rule.template).strip()
    else:
        template = str(
            rule_config.get("template") or numbering_config.get("template") or default_rule.template
        ).strip()
    counter_scope = _normalize_counter_scope(
        rule_config.get("counter_scope", numbering_config.get("counter_scope")),
        default_rule.counter_scope,
    )
    sequence_country_wildcard = default_rule.sequence_country_wildcard
    if "sequence_country_wildcard" in numbering_config:
        sequence_country_wildcard = _coerce_bool(
            numbering_config.get("sequence_country_wildcard"),
            sequence_country_wildcard,
        )
    if "sequence_country_wildcard" in rule_config:
        sequence_country_wildcard = _coerce_bool(
            rule_config.get("sequence_country_wildcard"),
            sequence_country_wildcard,
        )

    try:
        return _build_template_our_ref_scheme(
            template=template,
            code=code,
            country=country_value,
            counter_scope=counter_scope,
            sequence_country_wildcard=sequence_country_wildcard,
        )
    except ValueError as exc:
        raise NextOurRefError(
            code="invalid_numbering_rule",
            message=str(exc),
            status=400,
        ) from exc


def _parse_numbering_payload(value: object) -> tuple[dict[str, Any] | None, str | None]:
    if value is None or str(value).strip() == "":
        return {}, None
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc.msg}"
        if isinstance(parsed, dict):
            return parsed, None
        return None, "config must be a JSON object"
    return None, "config must be a JSON object"


def validate_our_ref_numbering_config_payload(value: object) -> dict[str, Any]:
    payload, parse_error = _parse_numbering_payload(value)
    if parse_error:
        return {"valid": False, "errors": [parse_error]}
    if payload is None:
        return {"valid": False, "errors": ["config must be a JSON object"]}

    errors: list[str] = []

    def _validate_template(path: str, raw_template: object) -> None:
        try:
            _parse_ref_template(str(raw_template or ""))
        except ValueError as exc:
            errors.append(f"{path}: {exc}")

    if "template" in payload:
        _validate_template("template", payload.get("template"))

    raw_codes = payload.get("codes")
    if raw_codes is not None:
        if not isinstance(raw_codes, dict):
            errors.append("codes must be an object")
        else:
            for key, code in raw_codes.items():
                if not _clean_code(code):
                    errors.append(f"codes.{key}: code must be 1-12 uppercase letters or digits")

    if "counter_scope" in payload:
        raw_scope = str(payload.get("counter_scope") or "").strip().lower()
        if raw_scope not in {"prefix", "country"}:
            errors.append("counter_scope: use prefix or country")

    raw_rules = payload.get("rules")
    if raw_rules is not None:
        if not isinstance(raw_rules, dict):
            errors.append("rules must be an object")
        else:
            for key, rule in raw_rules.items():
                if not isinstance(rule, dict):
                    errors.append(f"rules.{key}: rule must be an object")
                    continue
                if "template" in rule:
                    _validate_template(f"rules.{key}.template", rule.get("template"))
                if "code" in rule and not _clean_code(rule.get("code")):
                    errors.append(f"rules.{key}.code: code must be 1-12 uppercase letters or digits")
                if "country" in rule and not _clean_country(rule.get("country")):
                    errors.append(f"rules.{key}.country: country must be 1-8 letters or digits")
                if "counter_scope" in rule:
                    raw_scope = str(rule.get("counter_scope") or "").strip().lower()
                    if raw_scope not in {"prefix", "country"}:
                        errors.append(f"rules.{key}.counter_scope: use prefix or country")

    return {"valid": not errors, "errors": errors}


def generate_next_our_ref(
    *, division: str, matter_type: str, country: str | None = None, reserve: bool = False
) -> str:
    div = (division or "").strip().upper()
    typ = (matter_type or "").strip().upper()
    scheme = _build_our_ref_scheme(division=div, matter_type=typ, country=country)
    sequence_pattern = scheme.sequence_pattern or scheme.pattern
    max_num = _compute_max_our_ref_seq(scheme.prefix, sequence_pattern)

    if reserve:

        def _exists(seq_num: int) -> bool:
            candidate = scheme.build(seq_num)
            return (
                Matter.query.with_entities(Matter.matter_id)
                .filter(Matter.our_ref == candidate)
                .first()
                is not None
            )

        reserved = _reserve_next_seq(key=scheme.counter_key, max_seq=max_num, exists_fn=_exists)
        if reserved is not None:
            return scheme.build(reserved)

    return scheme.build(max_num + 1)
