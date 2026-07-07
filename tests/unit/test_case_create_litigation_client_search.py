from __future__ import annotations

import re
import uuid
from datetime import date

import pytest
from bs4 import BeautifulSoup

from app.models.client import Client
from app.models.user import User


def _extract_idempotency_key(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one('input[name="idempotency_key"]')
    return (node.get("value") if node else "") or ""


def test_litigation_create_form_has_optional_client_search_fields(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=LITIGATION")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    form = soup.select_one('form[data-disable-submit="1"]')
    assert form is not None

    required_client = form.select_one('input[name="client_name"][data-client-search="1"]')
    assert required_client is not None
    assert required_client.has_attr("required")

    optional_names = ("claimant_name", "respondent_name", "applicant_name")
    optional_found = 0
    for name in optional_names:
        node = form.select_one(f'input[name="{name}"][data-client-search="1"]')
        if node is None:
            continue
        optional_found += 1
        assert not node.has_attr("required")

    assert optional_found >= 1


@pytest.mark.parametrize(
    ("case_type", "optional_field"),
    [
        ("LITIGATION", "claimant_name"),
        ("MISC", "respondent_name"),
    ],
)
def test_create_accepts_optional_client_search_free_text(
    case_type: str, optional_field: str, authenticated_client, sample_user, db_session
):
    client = Client(name="Text", email="client@example.com")
    db_session.add(client)
    db_session.commit()
    client_id = client.id
    client_name = client.name

    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    username = (db_session.get(User, user_id).username or "").strip() or "testuser"

    get_resp = authenticated_client.get(f"/case/matter/createNewdivision=ETC&type={case_type}")
    assert get_resp.status_code == 200
    idem = _extract_idempotency_key(get_resp.get_data(as_text=True))
    assert idem

    payload = {
        "idempotency_key": idem,
        "category": case_type,
        "in_out_type": "",
        "division": "",
        "case_type": case_type,
        "our_ref": f"26LT{uuid.uuid4().hex[:4].upper()}",
        "client_name": client_name,
        "client_id": str(client_id),
        "retained_at": date.today().isoformat(),
        "manager": username,
        "attorney": username,
        optional_field: "Text Text",
        f"{optional_field}_id": "",
    }

    post_resp = authenticated_client.post(
        f"/case/matter/create?division=ETC&type={case_type}",
        data=payload,
        follow_redirects=False,
    )
    assert post_resp.status_code == 302
    location = post_resp.headers.get("Location") or ""
    assert re.match(r"^/case/[0-9a-f]{32}$", location)
