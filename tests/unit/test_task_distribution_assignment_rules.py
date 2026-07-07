import json


def test_validate_rules_rejects_invalid_distribute_to(monkeypatch, tmp_path):
    from app.utils import task_distribution_rules as tdr

    rules_path = tmp_path / "rules.invalid_action.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_action": {"distribute_to": "owner"},
                "rules": [
                    {
                        "id": "bad_action",
                        "priority": 1,
                        "match": {"name_ref_contains": ["TEST"]},
                        "action": {"distribute_to": "all_staf"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASK_DISTRIBUTION_RULES_PATH", str(rules_path))
    tdr._RULES_CACHE.clear()

    is_valid, errors = tdr.validate_rules()

    assert is_valid is False
    assert any("invalid distribute_to" in msg for msg in errors)


def test_resolve_distribution_decision_ignores_invalid_regex_rule(monkeypatch, tmp_path):
    from app.utils import task_distribution_rules as tdr

    rules_path = tmp_path / "rules.invalid_regex.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_action": {"distribute_to": "owner"},
                "rules": [
                    {
                        "id": "bad_regex",
                        "priority": 999,
                        "match": {"name_ref_regex": ["["]},
                        "action": {"distribute_to": "none"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASK_DISTRIBUTION_RULES_PATH", str(rules_path))
    tdr._RULES_CACHE.clear()

    decision = tdr.resolve_distribution_decision(
        category="WORK",
        name_ref="Text",
        name_free="Text",
        source=None,
    )

    assert decision.rule_id is None
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_ignores_unknown_match_field(monkeypatch, tmp_path):
    from app.utils import task_distribution_rules as tdr

    rules_path = tmp_path / "rules.unknown_match_field.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_action": {"distribute_to": "owner"},
                "rules": [
                    {
                        "id": "unknown_match",
                        "priority": 999,
                        "match": {"name_reff_contains": ["Text"]},
                        "action": {"distribute_to": "all_staff"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASK_DISTRIBUTION_RULES_PATH", str(rules_path))
    tdr._RULES_CACHE.clear()

    decision = tdr.resolve_distribution_decision(
        category="WORK",
        name_ref="Text",
        name_free="Text",
        source=None,
    )

    assert decision.rule_id is None
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_skips_duplicate_rule_id(monkeypatch, tmp_path):
    from app.utils import task_distribution_rules as tdr

    rules_path = tmp_path / "rules.duplicate_id.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_action": {"distribute_to": "owner"},
                "rules": [
                    {
                        "id": "dup_rule",
                        "priority": 1,
                        "match": {"name_ref_contains": ["DUPLICATE_CASE"]},
                        "action": {"distribute_to": "owner"},
                    },
                    {
                        "id": "dup_rule",
                        "priority": 999,
                        "match": {"name_ref_contains": ["DUPLICATE_CASE"]},
                        "action": {"distribute_to": "none"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASK_DISTRIBUTION_RULES_PATH", str(rules_path))
    tdr._RULES_CACHE.clear()

    decision = tdr.resolve_distribution_decision(
        category="WORK",
        name_ref="DUPLICATE_CASE",
        name_free="duplicate",
        source=None,
    )

    assert decision.rule_id == "dup_rule"
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_prefix_ignores_whitespace():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref="MGMT: STATUS_RED:OfficeActionDeadline",
        name_free="Office action deadline",
        source=None,
    )

    assert decision.rule_id == "status_red_mgmt_and_attorney"
    assert decision.distribute_to == "role_set"


def test_resolve_distribution_decision_work_filing_now_all_staff():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="WORK",
        name_ref="출원",
        name_free="출원 마감",
        source=None,
    )

    assert decision.rule_id == "name_ref_exact_all_staff"
    assert decision.distribute_to == "all_staff"


def test_resolve_distribution_decision_mgmt_exact_keyword_stays_manager_only():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref="출원",
        name_free="출원 마감",
        source=None,
    )

    assert decision.rule_id == "category_mgmt_manager_only"
    assert decision.distribute_to == "role_set"


def test_resolve_distribution_decision_notice_status_red_no_longer_all_staff():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="NOTICE",
        name_ref="NOTICE:STATUS_RED:XYZ",
        name_free="STATUS_RED",
        source=None,
    )

    assert decision.rule_id == "category_notice_owner"
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_source_force_distribute_yields_to_manager_rules():
    """source_force_distribute (priority=90) should NOT override manager-only rules (priority=100)."""
    from app.utils.task_distribution_rules import resolve_distribution_decision

    # When source is uspto_notice but name_ref matches manager_only_notice_ref_prefix (priority=100),
    # the manager-only rule should win because it has higher priority.
    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref="MGMT:NOTICE_SEND_3D:XYZ",
        name_free="Text Text",
        source="uspto_notice",
    )

    assert decision.rule_id in {
        "manager_only_notice_free_any_category",
        "manager_only_notice_ref_prefix",
        "manager_only_notice_free",
    }
    assert decision.distribute_to == "role_set"


def test_resolve_distribution_decision_source_force_applies_when_no_higher_rule():
    """source_force_distribute should still apply when no higher-priority rule matches."""
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="NOTICE",
        name_ref="NOTICE:GENERAL:ABC",
        name_free="Text Text",
        source="uspto_notice",
    )

    assert decision.rule_id == "source_force_distribute"
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_disabled_rule_is_ignored(monkeypatch, tmp_path):
    from app.utils import task_distribution_rules as tdr

    rules_path = tmp_path / "rules.disabled_rule.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_action": {"distribute_to": "owner"},
                "rules": [
                    {
                        "id": "disabled_high_priority",
                        "enabled": False,
                        "priority": 999,
                        "match": {"category": ["WORK"], "name_ref_contains": ["TEST"]},
                        "action": {"distribute_to": "none"},
                    },
                    {
                        "id": "enabled_low_priority",
                        "priority": 1,
                        "match": {"category": ["WORK"], "name_ref_contains": ["TEST"]},
                        "action": {"distribute_to": "all_staff"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASK_DISTRIBUTION_RULES_PATH", str(rules_path))
    tdr._RULES_CACHE.clear()

    decision = tdr.resolve_distribution_decision(
        category="WORK",
        name_ref="TEST:CASE",
        name_free=None,
        source=None,
    )

    assert decision.rule_id == "enabled_low_priority"
    assert decision.distribute_to == "all_staff"


def test_resolve_distribution_decision_category_uspto_oa_owner():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="USPTO_OA",
        name_ref="OA:LEGACY",
        name_free="Text",
        source=None,
    )

    assert decision.rule_id == "category_uspto_oa_owner"
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_office_action_main_notice_uses_hybrid_role_set():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="WORK",
        name_ref="NOTICE:OA:OA123",
        name_free="Text Text Text · Text",
        source="upload_automation",
    )

    assert decision.rule_id == "office_action_main_notice_mgmt_attorney_handler"
    assert decision.distribute_to == "role_set"
    assert {"manager", "attorney", "handler"}.issubset(set(decision.role_codes))


def test_resolve_distribution_decision_legacy_office_action_without_ref_uses_hybrid_role_set():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref=None,
        name_free="거절이유통지",
        source=None,
    )

    assert decision.rule_id == "legacy_office_action_name_free_mgmt_attorney_handler"
    assert decision.distribute_to == "role_set"
    assert {"manager", "attorney", "handler"}.issubset(set(decision.role_codes))


def test_resolve_distribution_decision_office_action_helper_docket_stays_work_owner():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="WORK",
        name_ref="NOTICE:OA:OA123:HDL",
        name_free="Text Text Text · Text (Text)",
        source="upload_automation",
    )

    assert decision.rule_id == "source_force_distribute"
    assert decision.distribute_to == "owner"


def test_resolve_distribution_decision_category_v2_limit_owner():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="V2_LIMIT",
        name_ref="",
        name_free="Text",
        source=None,
    )

    assert decision.rule_id == "category_v2_limit_owner"
    assert decision.distribute_to == "owner"


def test_flat_index_fallback_filters_requested_roles(monkeypatch, db_session):
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(tar, "_fetch_assignees_from_assignment", lambda **kwargs: [])
    monkeypatch.setattr(
        tar,
        "_fetch_assignees_from_flat_index",
        lambda matter_id: [
            tar.AssigneeInfo(1, "manager", None),
            tar.AssigneeInfo(2, "attorney", None),
            tar.AssigneeInfo(3, "handler", None),
        ],
    )

    db_session.info.pop("_task_assignment_cache", None)
    rows = tar._resolve_assignees_for_matter(
        matter_id="M-FILTER-1",
        role_codes=("manager", "mgmt", "attorney"),
    )
    assert {row.user_id for row in rows} == {1, 2}

    db_session.info.pop("_task_assignment_cache", None)
    rows = tar._resolve_assignees_for_matter(
        matter_id="M-FILTER-2",
        role_codes=("mgmt",),
    )
    assert {row.user_id for row in rows} == {1}

    db_session.info.pop("_task_assignment_cache", None)
    rows = tar._resolve_assignees_for_matter(
        matter_id="M-FILTER-3",
        role_codes=("retainer",),
    )
    assert {row.user_id for row in rows} == {2}

    db_session.info.pop("_task_assignment_cache", None)
    rows = tar._resolve_assignees_for_matter(
        matter_id="M-FILTER-4",
        role_codes=("staff",),
    )
    assert {row.user_id for row in rows} == {3}


def test_assignment_resolution_preserves_same_user_distinct_case_roles(monkeypatch, db_session):
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(
        tar,
        "_fetch_assignees_from_assignment",
        lambda **kwargs: [
            tar.AssigneeInfo(2, "manager", None),
            tar.AssigneeInfo(5, "attorney", None),
            tar.AssigneeInfo(5, "handler", None),
        ],
    )
    monkeypatch.setattr(tar, "_fetch_assignees_from_flat_index", lambda matter_id: [])

    db_session.info.pop("_task_assignment_cache", None)
    rows = tar._resolve_assignees_for_matter(
        matter_id="M-SAME-USER-ROLES",
        role_codes=("manager", "attorney", "handler"),
    )

    assert [(row.user_id, row.role_code) for row in rows] == [
        (2, "manager"),
        (5, "attorney"),
        (5, "handler"),
    ]


def test_role_set_fallback_does_not_leak_to_owner(monkeypatch):
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(tar, "resolve_user_id_by_staff_party_id", lambda staff_party_id: 101)
    monkeypatch.setattr(
        tar,
        "resolve_distribution_decision",
        lambda **kwargs: tar.DistributionDecision(
            distribute_to="role_set",
            role_codes=("manager", "mgmt"),
            rule_id="manager_only",
            priority=100,
        ),
    )
    monkeypatch.setattr(tar, "_resolve_assignees_for_matter", lambda **kwargs: [])

    rows = tar.resolve_assignees_for_task(
        matter_id="M-ROLESET-1",
        name_ref="MGMT:NOTICE:XYZ",
        name_free="Text Text",
        category="MGMT",
        owner_staff_party_id="owner-spid",
        fallback_user_id=None,
    )
    assert rows == []


def test_unknown_distribute_to_fails_closed(monkeypatch):
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(tar, "resolve_user_id_by_staff_party_id", lambda staff_party_id: 101)
    monkeypatch.setattr(
        tar,
        "resolve_distribution_decision",
        lambda **kwargs: tar.DistributionDecision(
            distribute_to="all_staf",
            role_codes=(),
            rule_id="typo_action",
            priority=10,
        ),
    )

    rows = tar.resolve_assignees_for_task(
        matter_id="M-ACTION-TYPO",
        name_ref="Text",
        name_free="Text",
        category="WORK",
        owner_staff_party_id="owner-spid",
        fallback_user_id=7,
        fallback_to_all=True,
    )
    assert rows == []


def test_all_staff_without_targets_returns_empty_without_unhandled_error_log(monkeypatch):
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(tar, "resolve_user_id_by_staff_party_id", lambda staff_party_id: None)
    monkeypatch.setattr(
        tar,
        "resolve_distribution_decision",
        lambda **kwargs: tar.DistributionDecision(
            distribute_to="all_staff",
            role_codes=(),
            rule_id="all_staff_test",
            priority=10,
        ),
    )
    monkeypatch.setattr(tar, "_resolve_assignees_for_matter", lambda **kwargs: [])

    error_messages = []
    monkeypatch.setattr(
        tar.logger,
        "error",
        lambda msg, *args, **kwargs: error_messages.append(str(msg)),
    )

    rows = tar.resolve_assignees_for_task(
        matter_id="M-ALL-STAFF-EMPTY",
        name_ref="Text",
        name_free="Text",
        category="WORK",
        owner_staff_party_id=None,
    )

    assert rows == []
    assert not any("Unhandled distribute_to" in msg for msg in error_messages)


def test_all_staff_without_targets_uses_fallback_user_id(monkeypatch):
    """When all_staff distribution finds no assignees, fallback_user_id should be used."""
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(tar, "resolve_user_id_by_staff_party_id", lambda staff_party_id: None)
    monkeypatch.setattr(
        tar,
        "resolve_distribution_decision",
        lambda **kwargs: tar.DistributionDecision(
            distribute_to="all_staff",
            role_codes=(),
            rule_id="all_staff_test",
            priority=10,
        ),
    )
    monkeypatch.setattr(tar, "_resolve_assignees_for_matter", lambda **kwargs: [])

    rows = tar.resolve_assignees_for_task(
        matter_id="M-ALL-STAFF-FB",
        name_ref="Text",
        name_free="Text",
        category="WORK",
        owner_staff_party_id=None,
        fallback_user_id=42,
    )

    assert len(rows) == 1
    assert rows[0].user_id == 42
    assert rows[0].role_code == "fallback"


def test_all_staff_owner_role_is_preferred_when_owner_already_in_staff(monkeypatch):
    from app.utils import task_assignment_rules as tar

    monkeypatch.setattr(tar, "resolve_user_id_by_staff_party_id", lambda staff_party_id: 5)
    monkeypatch.setattr(
        tar,
        "resolve_distribution_decision",
        lambda **kwargs: tar.DistributionDecision(
            distribute_to="all_staff",
            role_codes=(),
            rule_id="all_staff_test",
            priority=10,
        ),
    )
    monkeypatch.setattr(
        tar,
        "_resolve_assignees_for_matter",
        lambda **kwargs: [
            tar.AssigneeInfo(5, "manager", None),
            tar.AssigneeInfo(7, "attorney", None),
        ],
    )

    rows = tar.resolve_assignees_for_task(
        matter_id="M-ALL-STAFF-OWNER",
        name_ref="Text",
        name_free="Text",
        category="WORK",
        owner_staff_party_id="owner-spid",
    )

    assert len(rows) == 2
    assert rows[0].user_id == 5
    assert rows[0].role_code == "owner"


def test_status_red_rule_wins_over_source_force_for_mgmt():
    """MGMT:STATUS_RED: prefix rules (priority=95) beat source_force_distribute (priority=90)."""
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref="MGMT:STATUS_RED:OfficeActionDeadline",
        name_free="Office action deadline",
        source="upload_automation",
    )

    assert decision.rule_id == "status_red_mgmt_and_attorney"
    assert decision.distribute_to == "role_set"
    assert "attorney" in decision.role_codes


def test_gazette_publication_status_red_is_not_distributed():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref="MGMT:STATUS_RED:출원공고",
        name_free="출원공고",
        source="upload_automation",
    )

    assert decision.rule_id == "status_red_gazette_publication_no_work"
    assert decision.distribute_to == "none"


def test_gazette_publication_no_work_rule_is_exact_only():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT",
        name_ref="MGMT:STATUS_RED:출원공고:extra",
        name_free="출원공고",
        source="upload_automation",
    )

    assert decision.rule_id == "status_red_mgmt_and_attorney"
    assert decision.distribute_to == "role_set"


def test_foreign_filing_status_red_rule_is_mixed_not_manager_only():
    from app.utils.task_distribution_rules import resolve_distribution_decision

    decision = resolve_distribution_decision(
        category="MGMT_WORK",
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
        name_free="ForeignFilingDeadline",
        source=None,
    )

    assert decision.rule_id == "status_red_foreign_filing_mgmt_and_attorney"
    assert decision.distribute_to == "role_set"
    assert "manager" in decision.role_codes
    assert "attorney" in decision.role_codes


def test_is_manager_only_notice_task_uses_category_and_source(monkeypatch):
    from app.models.docket import DocketItem
    from app.utils import task_assignment_rules as tar

    captured = {}

    def _fake_decision(**kwargs):
        captured.update(kwargs)
        return tar.DistributionDecision(
            distribute_to="role_set",
            role_codes=("manager", "mgmt"),
            rule_id="manager_only",
            priority=100,
        )

    monkeypatch.setattr(tar, "resolve_distribution_decision", _fake_decision)
    docket_item = DocketItem(
        matter_id="M-CONTEXT-1",
        category="MGMT",
        name_ref="MGMT:NOTICE:ABC",
        name_free="Text Text",
        memo='{"source":"uspto_notice"}',
    )

    assert tar.is_manager_only_notice_task(docket_item) is True
    assert captured.get("category") == "MGMT"
    assert captured.get("source") == "uspto_notice"


def test_resolve_assignees_for_docket_passes_source(monkeypatch):
    from app.models.docket import DocketItem
    from app.utils import task_assignment_rules as tar

    captured = {}

    def _fake_resolve_assignees_for_task(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tar, "resolve_assignees_for_task", _fake_resolve_assignees_for_task)
    docket_item = DocketItem(
        matter_id="M-CONTEXT-2",
        category="NOTICE",
        name_ref="NOTICE:OA:1",
        name_free="Text Text",
        memo='{"source":"upload_automation"}',
    )

    rows = tar.resolve_assignees_for_docket(docket_item)

    assert rows == []
    assert captured.get("source") == "upload_automation"
    assert captured.get("fallback_to_all") is False


def test_extract_task_source_from_docket_uspto_patterns():
    from app.models.docket import DocketItem
    from app.utils import task_assignment_rules as tar

    uspto_notice_item = DocketItem(
        matter_id="M-SRC-1",
        category="WORK",
        name_ref="USPTO:OA123",
        name_free="Text",
        memo="Text: 2026-01-01",
    )
    upload_auto_item = DocketItem(
        matter_id="M-SRC-2",
        category="USPTO_OA",
        name_ref="OA:ABC123",
        name_free="Text",
        memo="legacy text memo",
    )

    assert tar._extract_task_source_from_docket(uspto_notice_item) == tar.TASK_SOURCE_USPTO_NOTICE
    assert (
        tar._extract_task_source_from_docket(upload_auto_item) == tar.TASK_SOURCE_UPLOAD_AUTOMATION
    )


def test_extract_task_source_from_docket_legacy_json_payload_without_source():
    from app.models.docket import DocketItem
    from app.utils import task_assignment_rules as tar

    upload_auto_item = DocketItem(
        matter_id="M-SRC-3",
        category="MGMT",
        name_ref="MGMT:STATUS_RED:OfficeActionDeadline",
        memo='{"auto":true,"trigger":"Office action","dispatch_date":"2026-01-01","events":[]}',
    )
    uspto_notice_item = DocketItem(
        matter_id="M-SRC-4",
        category="MGMT",
        name_ref="MGMT:STATUS_RED:NoticeAllowanceDeadline",
        memo='{"auto":true,"trigger":"Notice of allowance","dispatch_date":"2026-01-01"}',
    )

    assert (
        tar._extract_task_source_from_docket(upload_auto_item) == tar.TASK_SOURCE_UPLOAD_AUTOMATION
    )
    assert tar._extract_task_source_from_docket(uspto_notice_item) == tar.TASK_SOURCE_USPTO_NOTICE
