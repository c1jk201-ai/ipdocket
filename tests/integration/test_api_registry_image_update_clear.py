from __future__ import annotations

import uuid


def test_api_registry_image_clear_does_not_500(app, admin_client, db_session) -> None:
    """
    Regression: api/routes.py referenced _log_case_audit without defining it.
    This route should not 500 when clearing registry image.
    """
    from app.models.ip_records import Matter

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26TM0001US",
            right_name="Text",
            right_group="DOM",
            matter_type="TRADEMARK",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.commit()

    resp = admin_client.post(
        f"/api/cases/{mid}/registry-image",
        data={"clear": "1"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["cleared"] is True
    assert data["image"] == ""
