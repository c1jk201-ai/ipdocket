from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import String, and_, cast, func, not_, or_

_COMPACT_RE = re.compile(r"[\W_]+", flags=re.UNICODE)
_SQL_COMPACT_CHARS = (
    " ",
    "-",
    "_",
    "/",
    ".",
    "@",
)


@dataclass
class SearchExpressionGroup:
    terms: list[str] = field(default_factory=list)
    not_terms: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    not_fields: dict[str, list[str]] = field(default_factory=dict)

    def has_content(self) -> bool:
        return bool(self.terms or self.not_terms or self.fields or self.not_fields)


@dataclass
class SearchExpression:
    raw: str
    groups: list[SearchExpressionGroup] = field(default_factory=list)
    used_syntax: bool = False

    def has_positive_terms(self) -> bool:
        return any(group.terms or group.fields for group in self.groups)


_FALLBACK_TOKEN_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')


def normalize_search_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def compact_search_text(value: object) -> str:
    return _COMPACT_RE.sub("", normalize_search_text(value))


def _tokenize_search_expression(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        out: list[str] = []
        for quoted_double, quoted_single, plain in _FALLBACK_TOKEN_RE.findall(text):
            token = quoted_double or quoted_single or plain
            token = str(token or "").strip()
            if token:
                out.append(token)
        return out


def _append_search_value(target: dict[str, list[str]], key: str, value: str) -> None:
    raw_key = str(key or "").strip()
    raw_value = str(value or "").strip()
    if not raw_key or not raw_value:
        return
    target.setdefault(raw_key, []).append(raw_value)


def extract_positive_search_terms(
    expression: SearchExpression,
    *,
    allowed_fields: set[str] | None = None,
    limit: int | None = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in expression.groups:
        for term in group.terms:
            value = str(term or "").strip()
            if value and value not in seen:
                out.append(value)
                seen.add(value)
                if limit and len(out) >= limit:
                    return out
        for field_name, values in group.fields.items():
            if allowed_fields is not None and field_name not in allowed_fields:
                continue
            for value in values:
                raw = str(value or "").strip()
                if raw and raw not in seen:
                    out.append(raw)
                    seen.add(raw)
                    if limit and len(out) >= limit:
                        return out
    return out


def parse_search_expression(
    raw: object,
    *,
    field_aliases: dict[str, str] | None = None,
) -> SearchExpression:
    text = str(raw or "").strip()
    tokens = _tokenize_search_expression(text)
    aliases = {
        str(key or "").strip().casefold(): str(value or "").strip()
        for key, value in (field_aliases or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    expression = SearchExpression(raw=text)
    if not tokens:
        return expression

    groups: list[SearchExpressionGroup] = []
    current = SearchExpressionGroup()
    pending_not = False
    used_syntax = bool(re.search(r"""["']""", text))
    idx = 0

    while idx < len(tokens):
        token = str(tokens[idx] or "").strip()
        idx += 1
        if not token:
            continue

        upper = token.upper()
        if upper in {"OR", "|"}:
            if current.has_content():
                groups.append(current)
                current = SearchExpressionGroup()
            used_syntax = True
            pending_not = False
            continue

        if upper == "NOT":
            pending_not = True
            used_syntax = True
            continue

        negate = pending_not
        pending_not = False

        if token.startswith("+") and len(token) > 1:
            token = token[1:].strip()
            used_syntax = True
        elif token.startswith("-") and len(token) > 1:
            token = token[1:].strip()
            negate = True
            used_syntax = True

        if not token:
            continue

        parsed_field = False
        for sep in (":", "="):
            alias_key = ""
            field_value = ""
            if token.endswith(sep):
                alias_key = token[:-1].strip()
                if idx < len(tokens):
                    field_value = str(tokens[idx] or "").strip()
                    idx += 1
                    used_syntax = True
            elif sep in token:
                alias_key, field_value = token.split(sep, 1)
                alias_key = alias_key.strip()
                field_value = field_value.strip()
                used_syntax = True

            if not alias_key:
                continue

            canonical = aliases.get(alias_key.casefold())
            if not canonical:
                continue

            target = current.not_fields if negate else current.fields
            _append_search_value(target, canonical, field_value)
            parsed_field = True
            break

        if parsed_field:
            continue

        if negate:
            current.not_terms.append(token)
        else:
            current.terms.append(token)

    if current.has_content():
        groups.append(current)

    if not groups and text:
        groups.append(SearchExpressionGroup(terms=[text]))

    expression.groups = groups
    expression.used_syntax = used_syntax or any(
        group.not_terms or group.fields or group.not_fields for group in groups
    )
    return expression


def matches_search_expression(
    search_text: object,
    expression: SearchExpression,
    *,
    field_values: dict[str, object] | None = None,
) -> bool:
    if not expression.groups:
        return text_matches_query(search_text, expression.raw)

    def _values_for(field_name: str) -> list[str]:
        if not field_values or field_name not in field_values:
            return []
        raw = field_values.get(field_name)
        if raw is None:
            return []
        if isinstance(raw, (list, tuple, set)):
            return [str(item or "").strip() for item in raw if str(item or "").strip()]
        value = str(raw or "").strip()
        return [value] if value else []

    for group in expression.groups:
        ok = True
        for term in group.terms:
            if not text_matches_query(search_text, term):
                ok = False
                break
        if not ok:
            continue

        for term in group.not_terms:
            if text_matches_query(search_text, term):
                ok = False
                break
        if not ok:
            continue

        for field_name, values in group.fields.items():
            haystacks = _values_for(field_name)
            if not haystacks:
                ok = False
                break
            if not all(
                any(text_matches_query(haystack, value) for haystack in haystacks)
                for value in values
            ):
                ok = False
                break
        if not ok:
            continue

        for field_name, values in group.not_fields.items():
            haystacks = _values_for(field_name)
            if not haystacks:
                continue
            if any(
                any(text_matches_query(haystack, value) for haystack in haystacks)
                for value in values
            ):
                ok = False
                break
        if ok:
            return True

    return False


def build_sqlalchemy_search_filter(
    expression: SearchExpression,
    *,
    general_term_builder: Callable[[str], object] | None,
    field_builders: dict[str, Callable[[str], object]] | None = None,
):
    if not expression.groups:
        if not callable(general_term_builder):
            return None
        return general_term_builder(expression.raw)

    field_builders = field_builders or {}
    groups = []
    for group in expression.groups:
        clauses = []
        invalid_group = False

        for term in group.terms:
            if not callable(general_term_builder):
                invalid_group = True
                break
            clause = general_term_builder(term)
            if clause is None:
                invalid_group = True
                break
            clauses.append(clause)
        if invalid_group:
            continue

        for term in group.not_terms:
            if not callable(general_term_builder):
                invalid_group = True
                break
            clause = general_term_builder(term)
            if clause is None:
                invalid_group = True
                break
            clauses.append(not_(clause))
        if invalid_group:
            continue

        for field_name, values in group.fields.items():
            builder = field_builders.get(field_name)
            if not callable(builder):
                invalid_group = True
                break
            for value in values:
                clause = builder(value)
                if clause is None:
                    invalid_group = True
                    break
                clauses.append(clause)
            if invalid_group:
                break
        if invalid_group:
            continue

        for field_name, values in group.not_fields.items():
            builder = field_builders.get(field_name)
            if not callable(builder):
                invalid_group = True
                break
            for value in values:
                clause = builder(value)
                if clause is None:
                    invalid_group = True
                    break
                clauses.append(not_(clause))
            if invalid_group:
                break

        if clauses:
            groups.append(and_(*clauses))

    if not groups:
        return None
    return or_(*groups)


def text_matches_query(search_text: object, search_query: object) -> bool:
    query = normalize_search_text(search_query)
    if not query:
        return True

    haystack = normalize_search_text(search_text)
    if not haystack:
        return False

    if query in haystack:
        return True

    haystack_compact = compact_search_text(haystack)
    query_compact = compact_search_text(query)
    if query_compact and query_compact in haystack_compact:
        return True

    tokens = [tok for tok in query.split() if tok]
    if len(tokens) > 1:
        return all(
            normalize_search_text(token) in haystack
            or (compact_search_text(token) and compact_search_text(token) in haystack_compact)
            for token in tokens
        )

    return False


def _raw_sql_compact_expr(expr: str) -> str:
    out = f"LOWER(COALESCE({expr}, ''))"
    for ch in _SQL_COMPACT_CHARS:
        escaped = ch.replace("'", "''")
        out = f"REPLACE({out}, '{escaped}', '')"
    return out


def sql_raw_ci_contains_any(
    expressions: list[str] | tuple[str, ...], value: object
) -> tuple[str, list[str]]:
    needle = normalize_search_text(value)
    exprs = [str(expr or "").strip() for expr in (expressions or []) if str(expr or "").strip()]
    if not needle or not exprs:
        return "", []

    params: list[str] = []
    clauses = [f"LOWER(COALESCE({expr}, '')) LIKE ?" for expr in exprs]
    params.extend([f"%{needle}%"] * len(exprs))

    compact = compact_search_text(needle)
    if compact and len(compact) >= 2:
        clauses.extend(f"{_raw_sql_compact_expr(expr)} LIKE ?" for expr in exprs)
        params.extend([f"%{compact}%"] * len(exprs))

    return "(" + " OR ".join(clauses) + ")", params


def sqlalchemy_contains_query(field, query: object):
    raw = normalize_search_text(query)
    text_expr = func.lower(cast(func.coalesce(field, ""), String))
    clauses = [text_expr.like(f"%{raw}%")]

    compact_expr = text_expr
    for ch in _SQL_COMPACT_CHARS:
        compact_expr = func.replace(compact_expr, ch, "")

    compact = compact_search_text(raw)
    if compact and len(compact) >= 2:
        clauses.append(compact_expr.like(f"%{compact}%"))

    tokens = [tok for tok in raw.split() if tok]
    if len(tokens) > 1:
        token_clauses = []
        for token in tokens:
            compact_token = compact_search_text(token)
            if compact_token and compact_token != token:
                token_clauses.append(
                    or_(
                        text_expr.like(f"%{token}%"),
                        compact_expr.like(f"%{compact_token}%"),
                    )
                )
            else:
                token_clauses.append(text_expr.like(f"%{token}%"))
        clauses.append(and_(*token_clauses))

    return or_(*clauses)
