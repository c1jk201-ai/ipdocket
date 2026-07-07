from __future__ import annotations


def test_legacy_case_relations_marks_deprecated_api(admin_client, db_session):
    from app.models.case import Case

    case = Case(ref_no="LEGACY-REL-1", title="Legacy relations")
    db_session.add(case)
    db_session.commit()

    res = admin_client.get(f"/api/case/{case.id}/relations")

    assert res.status_code == 200
    assert res.headers["Deprecation"] == "true"
    assert res.headers["X-IPM-Legacy-Compat"] == "api-case-id"
    assert "/api/cases/<matter_id>/summary" in res.headers["Link"]
