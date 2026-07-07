from __future__ import annotations

import re

from sqlalchemy import text as sa_text
from sqlalchemy.sql.elements import TextClause

# Keep in sync with the raw SQL guard in policy_engine.
_RAW_SQL_GUARD_TABLE_TOKENS = (
    "matter",
    "docket",
    "workflow",
    "file_asset",
    "communication",
    "office_action",
)

_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\\*.*?\\*/", re.DOTALL)

# NOTE:
# This is a best-effort detector used by the raw SQL guard. We intentionally avoid matching
# tokens inside string literals (e.g. table_name='matter') by scanning table *identifiers*
# that appear after common SQL table-reference keywords.
_SQL_TABLE_REF_RE = re.compile(
    r"""
    (?:
        \binsert\s+into\b
        | \bmerge\s+into\b
        | \bupdate\b
        | \bdelete\s+from\b
        | \bfrom\b
        | \bjoin\b
        | \busing\b
    )
    \s+
    (?:only\s+)?        # postgres: FROM ONLY table
    (?:lateral\s+)?     # postgres: JOIN LATERAL ...
    (?P<ident>[A-Za-z0-9_."`\[\]]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_restricted_raw_sql(statement) -> bool:
    try:
        if not isinstance(statement, TextClause):
            return False
        sql = str(statement.text) or ""
        if not sql:
            return False
        # Strip SQL comments to reduce false positives and improve table-ref scanning.
        sql = _SQL_BLOCK_COMMENT_RE.sub(" ", sql)
        sql = _SQL_LINE_COMMENT_RE.sub(" ", sql)

        for match in _SQL_TABLE_REF_RE.finditer(sql):
            ident = (match.group("ident") or "").strip()
            if not ident:
                continue
            # Normalize schema-qualified identifiers and strip common quoting.
            last = ident.split(".")[-1].strip()
            last = last.replace('"', "").replace("`", "").replace("[", "").replace("]", "")
            last = last.lower()
            if any(token in last for token in _RAW_SQL_GUARD_TABLE_TOKENS):
                return True
        return False
    except Exception:
        return False


def policy_text(sql: str) -> TextClause:
    clause = sa_text(sql)
    if looks_like_restricted_raw_sql(clause):
        return clause.execution_options(
            policy_bypass=True,
            policy_bypass_reason="legacy_policy_text_auto_bypass",
            policy_bypass_scope="restricted_table_auto_detected",
        )
    return clause


def policy_bypass_text(*, sql: str, reason: str, scope: str) -> TextClause:
    clean_reason = str(reason or "").strip()
    clean_scope = str(scope or "").strip()
    if not clean_reason:
        raise ValueError("policy_bypass_text requires reason")
    if not clean_scope:
        raise ValueError("policy_bypass_text requires scope")
    return sa_text(sql).execution_options(
        policy_bypass=True,
        policy_bypass_reason=clean_reason,
        policy_bypass_scope=clean_scope,
    )
