from __future__ import annotations


def test_out_trademark_identifier_sync_accepts_legacy_keyword(monkeypatch):
    from app.blueprints.case import helpers as case_helpers

    captured: dict[str, object] = {}

    def _fake_bulk_replace_matter_identifiers(*, mid: str, source_column: str, id_pairs):
        captured["mid"] = mid
        captured["source_column"] = source_column
        captured["id_pairs"] = list(id_pairs)

    monkeypatch.setattr(
        case_helpers, "_bulk_replace_matter_identifiers", _fake_bulk_replace_matter_identifiers
    )

    case_helpers._sync_matter_identifiers_from_out_trademark(
        matter_id="m-out-1",
        out_trademark={"application_no": "OUT-APP-001"},
    )

    assert captured["mid"] == "m-out-1"
    assert captured["source_column"] == "outgoing_trademark"
    assert ("Application No.", "OUT-APP-001") in captured["id_pairs"]


def test_out_trademark_event_sync_accepts_canonical_keyword(monkeypatch):
    from app.blueprints.case import helpers as case_helpers

    captured: dict[str, object] = {}

    def _fake_bulk_replace_matter_events(*, mid: str, src: str, event_pairs):
        captured["mid"] = mid
        captured["src"] = src
        captured["event_pairs"] = list(event_pairs)

    monkeypatch.setattr(
        case_helpers, "_bulk_replace_matter_events", _fake_bulk_replace_matter_events
    )

    case_helpers._sync_matter_events_from_out_trademark(
        matter_id="m-out-2",
        out_tm={"term_expiry_date": "2035-09-14"},
    )

    assert captured["mid"] == "m-out-2"
    assert captured["src"] == "form:outgoing_trademark"
    assert (" Period ", "2035-09-14") in captured["event_pairs"]


def test_incoming_trademark_identifier_sync_accepts_legacy_keyword(monkeypatch):
    from app.blueprints.case import helpers as case_helpers

    captured: dict[str, object] = {}

    def _fake_bulk_replace_matter_identifiers(*, mid: str, source_column: str, id_pairs):
        captured["mid"] = mid
        captured["source_column"] = source_column
        captured["id_pairs"] = list(id_pairs)

    monkeypatch.setattr(
        case_helpers, "_bulk_replace_matter_identifiers", _fake_bulk_replace_matter_identifiers
    )

    case_helpers._sync_matter_identifiers_from_inc_trademark(
        matter_id="m-inc-1",
        inc_trademark={"application_no": "INC-APP-001"},
    )

    assert captured["mid"] == "m-inc-1"
    assert captured["source_column"] == "incoming_trademark"
    assert ("Application No.", "INC-APP-001") in captured["id_pairs"]


def test_incoming_trademark_event_sync_accepts_canonical_keyword(monkeypatch):
    from app.blueprints.case import helpers as case_helpers

    captured: dict[str, object] = {}

    def _fake_bulk_replace_matter_events(*, mid: str, src: str, event_pairs):
        captured["mid"] = mid
        captured["src"] = src
        captured["event_pairs"] = list(event_pairs)

    monkeypatch.setattr(
        case_helpers, "_bulk_replace_matter_events", _fake_bulk_replace_matter_events
    )

    case_helpers._sync_matter_events_from_inc_trademark(
        matter_id="m-inc-2",
        inc_tm={"term_expiry_date": "2032-01-01"},
    )

    assert captured["mid"] == "m-inc-2"
    assert captured["src"] == "form:incoming_trademark"
    assert (" Period ", "2032-01-01") in captured["event_pairs"]
