from __future__ import annotations

import uuid
from datetime import date


class _DummySession(dict):
    modified: bool = False


def _build_cmd(*, sample_user, division: str, case_type: str, client_id: int, form_data: dict):
    from app.services.matter.matter_domain import MatterCreateCommand

    payload = {
        "retained_at": date.today().isoformat(),
        "client_name": "Test Client",
        "client_id": str(client_id),
        **form_data,
    }
    return MatterCreateCommand(
        division=division,
        case_type=case_type,
        form_data=payload,
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )


def test_create_out_patent_keeps_out_storage(app, db_session, sample_user) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="OUT",
        case_type="PATENT",
        client_id=client.id,
        form_data={
            "our_ref": f"TEST{uuid.uuid4().hex[:8].upper()}US",
            "application_country": "US",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    matter = Matter.query.get(str(res.matter_id))
    assert matter is not None
    assert matter.right_group == "OUT"
    assert matter.matter_type == "PATENT"


def test_create_out_patent_infers_application_country_from_our_ref(
    app, db_session, sample_user
) -> None:
    from app.models.client import Client
    from app.models.ip_records import MatterCustomField
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="OUT",
        case_type="PATENT",
        client_id=client.id,
        form_data={
            "our_ref": f"{date.today().year % 100:02d}PO9876US",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    row = MatterCustomField.query.filter_by(
        matter_id=str(res.matter_id),
        namespace="outgoing_patent",
    ).first()
    assert row is not None
    assert (row.data or {}).get("application_country") == "US"


def test_create_rejects_six_digit_year_in_custom_date(app, db_session, sample_user) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()
    our_ref = f"{date.today().year % 100:02d}DD{uuid.uuid4().hex[:4].upper()}US"

    cmd = _build_cmd(
        sample_user=sample_user,
        division="DOM",
        case_type="DESIGN",
        client_id=client.id,
        form_data={
            "our_ref": our_ref,
            "novelty_grace_date": "222222-02-02",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))

    assert res.success is False
    assert "Invalid date format" in (res.error or "")
    assert Matter.query.filter_by(our_ref=our_ref).first() is None


def test_create_pct_persists_etc_storage(app, db_session, sample_user) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="OUT",
        case_type="PCT",
        client_id=client.id,
        form_data={"our_ref": f"{date.today().year % 100:02d}PD{uuid.uuid4().hex[:4].upper()}PCT"},
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    matter = Matter.query.get(str(res.matter_id))
    assert matter is not None
    assert matter.right_group == "ETC"
    assert matter.matter_type == "PCT"


def test_create_madrid_persists_etc_storage(app, db_session, sample_user) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterCustomField
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="OUT",
        case_type="TRADEMARK",
        client_id=client.id,
        form_data={
            "our_ref": f"{date.today().year % 100:02d}TO{uuid.uuid4().hex[:4].upper()}US",
            "application_country": "US",
            "app_route": "Madrid",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    matter = Matter.query.get(str(res.matter_id))
    assert matter is not None
    assert matter.right_group == "ETC"
    assert matter.matter_type == "MADRID"

    row = MatterCustomField.query.filter_by(
        matter_id=str(res.matter_id),
        namespace="outgoing_trademark",
    ).first()
    assert row is not None
    assert (row.data or {}).get("app_route") == "Madrid"


def test_create_out_trademark_without_madrid_marker_keeps_out_storage(
    app, db_session, sample_user
) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="OUT",
        case_type="TRADEMARK",
        client_id=client.id,
        form_data={
            "our_ref": f"{date.today().year % 100:02d}TO{uuid.uuid4().hex[:4].upper()}JP",
            "application_country": "JP",
            "app_route": "Paris",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    matter = Matter.query.get(str(res.matter_id))
    assert matter is not None
    assert matter.right_group == "OUT"
    assert matter.matter_type == "TRADEMARK"


def test_create_out_design_without_hague_marker_keeps_out_storage(
    app, db_session, sample_user
) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="OUT",
        case_type="DESIGN",
        client_id=client.id,
        form_data={
            "our_ref": f"{date.today().year % 100:02d}DO{uuid.uuid4().hex[:4].upper()}JP",
            "application_country": "JP",
            "app_route": "Paris",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    matter = Matter.query.get(str(res.matter_id))
    assert matter is not None
    assert matter.right_group == "OUT"
    assert matter.matter_type == "DESIGN"


def test_create_copyright_persists_etc_storage(app, db_session, sample_user) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = _build_cmd(
        sample_user=sample_user,
        division="",
        case_type="MISC",
        client_id=client.id,
        form_data={
            "our_ref": f"{date.today().year % 100:02d}ET{uuid.uuid4().hex[:4].upper()}US",
            "right_type": "Copyright",
            "case_kind": "Copyright",
            "right_name": "Copyright Matter",
        },
    )

    res = MatterCreateApplyUseCase().execute(cmd, SessionIdempotencyStore(_DummySession()))
    assert res.success is True

    matter = Matter.query.get(str(res.matter_id))
    assert matter is not None
    assert matter.right_group == "ETC"
    assert matter.matter_type == "COPYRIGHT"
