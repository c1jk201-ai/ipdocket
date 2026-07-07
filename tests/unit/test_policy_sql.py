import pytest
from sqlalchemy import text as sa_text

from app.utils.policy_sql import looks_like_restricted_raw_sql, policy_bypass_text, policy_text


def test_looks_like_restricted_raw_sql_detects_table_references():
    assert looks_like_restricted_raw_sql(sa_text("SELECT 1 FROM matter"))
    assert looks_like_restricted_raw_sql(sa_text("SELECT 1 FROM matter_identifier"))
    assert looks_like_restricted_raw_sql(sa_text("SELECT 1 FROM public.file_asset"))
    assert looks_like_restricted_raw_sql(sa_text('UPDATE "office_action" SET done_date = NULL'))
    assert looks_like_restricted_raw_sql(
        sa_text("INSERT INTO communication_file_asset(comm_id) VALUES ('x')")
    )
    assert looks_like_restricted_raw_sql(sa_text("DELETE FROM docket_item WHERE 1=1"))


def test_looks_like_restricted_raw_sql_ignores_string_literals_and_comments():
    # Token appears only as a string literal (should not be treated as touching restricted tables).
    assert not looks_like_restricted_raw_sql(
        sa_text("SELECT 1 FROM information_schema.tables WHERE table_name = 'matter'")
    )
    assert not looks_like_restricted_raw_sql(sa_text("SELECT 'communication' AS label"))
    assert not looks_like_restricted_raw_sql(
        sa_text("SELECT 1 FROM some_table WHERE note LIKE '%office_action%'")
    )

    # Token appears only in comments.
    assert not looks_like_restricted_raw_sql(
        sa_text(
            """
            -- matter
            SELECT 1 FROM information_schema.tables
            """
        )
    )
    assert not looks_like_restricted_raw_sql(
        sa_text(
            """
            /* communication */
            SELECT 1 FROM information_schema.tables
            """
        )
    )


def test_policy_text_sets_policy_bypass_only_when_restricted():
    restricted = policy_text("SELECT 1 FROM matter")
    assert restricted.get_execution_options().get("policy_bypass") is True
    assert restricted.get_execution_options().get("policy_bypass_reason")
    assert restricted.get_execution_options().get("policy_bypass_scope")

    not_restricted = policy_text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'matter'"
    )
    assert not not_restricted.get_execution_options().get("policy_bypass")


def test_policy_bypass_text_requires_auditable_reason_and_scope():
    clause = policy_bypass_text(
        sql="SELECT 1 FROM matter",
        reason="unit test restricted read",
        scope="matter:read",
    )
    opts = clause.get_execution_options()
    assert opts.get("policy_bypass") is True
    assert opts.get("policy_bypass_reason") == "unit test restricted read"
    assert opts.get("policy_bypass_scope") == "matter:read"

    with pytest.raises(ValueError):
        policy_bypass_text(sql="SELECT 1 FROM matter", reason="", scope="matter:read")
    with pytest.raises(ValueError):
        policy_bypass_text(sql="SELECT 1 FROM matter", reason="unit test", scope="")
