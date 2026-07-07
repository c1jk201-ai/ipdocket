from __future__ import annotations

import json
import uuid
from datetime import date

from app.models.case import Case
from app.models.matter import Matter
from app.models.system_config import SystemConfig
from app.services.case.case_numbering import (
    CASE_OUR_REF_NUMBERING_CONFIG_KEY,
    generate_next_our_ref,
    validate_our_ref_numbering_config_payload,
)


def _yy() -> str:
    return f"{date.today().year % 100:02d}"


def _today_yymmdd() -> str:
    today = date.today()
    return f"{today.year % 100:02d}{today.month:02d}{today.day:02d}"


def _us_patent_prefix() -> str:
    return f"USP{_yy()}"


def _us_trademark_prefix() -> str:
    return f"UST{_yy()}"


def _dom_patent_prefix() -> str:
    return f"USP{_yy()}"


def _us_out_tm_prefix() -> str:
    return f"USFT{_yy()}"


def _pct_prefix() -> str:
    return f"PCT{_yy()}"


def _dom_litigation_prefix() -> str:
    return f"L{_yy()}"


def _dom_misc_prefix() -> str:
    return f"M{_yy()}"


def _us_misc_prefix() -> str:
    return f"M{_yy()}"


def _madrid_prefix() -> str:
    return f"MAD{_yy()}"


def _hague_prefix() -> str:
    return f"HAG{_yy()}"


def _copyright_prefix() -> str:
    return f"CR{_yy()}"


def test_case_next_our_ref_preview_does_not_consume_counter(admin_client, db_session):
    prefix = _dom_patent_prefix()
    existing_ref = f"{prefix}0138"
    expected_next = f"{prefix}0139"
    counter_key = f"our_ref_counter:{prefix}:US"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="DOM",
            matter_type="PATENT",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    for _ in range(2):
        resp = admin_client.get("/case/api/next_our_ref?division=DOM&type=PATENT&country=US")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert payload.get("our_ref") == expected_next
        assert (payload.get("value") or {}).get("our_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_case_next_our_ref_preview_defaults_dom_country_to_us(admin_client, db_session):
    prefix = _us_patent_prefix()
    existing_ref = f"{prefix}0138"
    expected_next = f"{prefix}0139"
    counter_key = f"our_ref_counter:{prefix}:US"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="DOM",
            matter_type="PATENT",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?division=DOM&type=PATENT")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == expected_next
    assert (payload.get("value") or {}).get("our_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_case_next_our_ref_preview_dom_trademark_uses_current_short_code_rule(
    admin_client, db_session
):
    prefix = _us_trademark_prefix()
    existing_ref = f"{prefix}0001"
    expected_next = f"{prefix}0002"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="DOM",
            matter_type="TRADEMARK",
        )
    )
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?division=DOM&type=TRADEMARK&country=US")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == expected_next
    assert (payload.get("value") or {}).get("our_ref") == expected_next


def test_case_next_our_ref_preview_out_uses_country_scoped_default_sequence(
    admin_client, db_session
):
    us_prefix = _us_out_tm_prefix()
    yy = _yy()

    for ref, country in (
        (f"{us_prefix}0101", "US"),
        (f"CNFT{yy}0103", "CN"),
        (f"JPFT{yy}0106", "JP"),
    ):
        db_session.add(
            Matter(
                matter_id=uuid.uuid4().hex,
                our_ref=ref,
                right_group="OUT",
                matter_type="TRADEMARK",
                status_red=country,
            )
        )
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?division=OUT&type=TRADEMARK&country=US")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == f"{us_prefix}0102"
    assert (payload.get("value") or {}).get("our_ref") == f"{us_prefix}0102"


def test_generate_next_our_ref_reserve_out_uses_country_scoped_default_sequence(db_session):
    prefix = _us_out_tm_prefix()

    for ref in (
        f"{prefix}0102",
        f"CNFT{_yy()}0103",
        f"JPFT{_yy()}0106",
    ):
        db_session.add(
            Matter(
                matter_id=uuid.uuid4().hex,
                our_ref=ref,
                right_group="OUT",
                matter_type="TRADEMARK",
            )
        )
    db_session.add(SystemConfig(key=f"our_ref_counter:{prefix}:US", value="103"))
    db_session.commit()

    next_ref = generate_next_our_ref(
        division="OUT",
        matter_type="TRADEMARK",
        country="US",
        reserve=True,
    )

    assert next_ref == f"{prefix}0104"
    counter = db_session.get(SystemConfig, f"our_ref_counter:{prefix}:US")
    assert counter is not None
    assert counter.value == "104"


def test_case_next_our_ref_preview_supports_pct(admin_client, db_session):
    prefix = _pct_prefix()
    existing_ref = f"{prefix}0103"
    expected_next = f"{prefix}0104"
    counter_key = f"our_ref_counter:{prefix}:PCT"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="OUT",
            matter_type="PCT",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    for _ in range(2):
        resp = admin_client.get("/case/api/next_our_ref?division=OUT&type=PCT&country=PCT")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert payload.get("our_ref") == expected_next
        assert (payload.get("value") or {}).get("our_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_case_next_our_ref_preview_supports_litigation(admin_client, db_session):
    prefix = _dom_litigation_prefix()
    existing_ref = f"{prefix}0002"
    expected_next = f"{prefix}0003"
    counter_key = f"our_ref_counter:{prefix}"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            matter_type="LITIGATION",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    for _ in range(2):
        resp = admin_client.get("/case/api/next_our_ref?type=LITIGATION&country=US")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert payload.get("our_ref") == expected_next
        assert (payload.get("value") or {}).get("our_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_case_next_our_ref_preview_supports_misc(admin_client, db_session):
    prefix = _dom_misc_prefix()
    existing_ref = f"{prefix}0101"
    expected_next = f"{prefix}0102"
    counter_key = f"our_ref_counter:{prefix}"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            matter_type="MISC",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    for _ in range(2):
        resp = admin_client.get("/case/api/next_our_ref?type=MISC&country=US")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert payload.get("our_ref") == expected_next
        assert (payload.get("value") or {}).get("our_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_case_next_our_ref_preview_defaults_misc_country_to_us(admin_client, db_session):
    prefix = _us_misc_prefix()
    existing_ref = f"{prefix}0101"
    expected_next = f"{prefix}0102"
    counter_key = f"our_ref_counter:{prefix}"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            matter_type="MISC",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?type=MISC")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == expected_next
    assert (payload.get("value") or {}).get("our_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_case_next_our_ref_preview_supports_madrid(admin_client, db_session):
    prefix = _madrid_prefix()
    existing_ref = f"{prefix}0007"
    expected_next = f"{prefix}0008"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="ETC",
            matter_type="MADRID",
        )
    )
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?division=ETC&type=MADRID")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == expected_next
    assert (payload.get("value") or {}).get("our_ref") == expected_next


def test_case_next_our_ref_preview_supports_hague(admin_client, db_session):
    prefix = _hague_prefix()
    existing_ref = f"{prefix}0007"
    expected_next = f"{prefix}0008"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="ETC",
            matter_type="HAGUE",
        )
    )
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?division=ETC&type=HAGUE")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == expected_next
    assert (payload.get("value") or {}).get("our_ref") == expected_next


def test_case_next_our_ref_preview_supports_copyright(admin_client, db_session):
    prefix = _copyright_prefix()
    existing_ref = f"{prefix}0007"
    expected_next = f"{prefix}0008"

    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=existing_ref,
            right_group="ETC",
            matter_type="COPYRIGHT",
        )
    )
    db_session.commit()

    resp = admin_client.get("/case/api/next_our_ref?division=ETC&type=COPYRIGHT")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("our_ref") == expected_next
    assert (payload.get("value") or {}).get("our_ref") == expected_next


def test_special_etc_default_rule_is_not_overridden_by_global_template(db_session):
    config = {
        "template": "{country}{code}YY{seq:0000}",
        "codes": {
            "OUT:TRADEMARK": "FT",
        },
    }
    db_session.add(SystemConfig(key=CASE_OUR_REF_NUMBERING_CONFIG_KEY, value=json.dumps(config)))
    db_session.commit()

    assert generate_next_our_ref(division="ETC", matter_type="MADRID") == f"MAD{_yy()}0001"


def test_configured_rule_supports_appd_style_reference(db_session):
    yy = _yy()
    config = {
        "rules": {
            "DOM:PATENT": {
                "template": "AP{code}YY{seq:0000}{country}",
                "code": "PD",
                "counter_scope": "country",
            }
        }
    }
    db_session.add(SystemConfig(key=CASE_OUR_REF_NUMBERING_CONFIG_KEY, value=json.dumps(config)))
    db_session.add(
        Matter(
            matter_id=uuid.uuid4().hex,
            our_ref=f"APPD{yy}0110US",
            right_group="DOM",
            matter_type="PATENT",
        )
    )
    db_session.commit()

    assert generate_next_our_ref(division="DOM", matter_type="PATENT", country="US") == (
        f"APPD{yy}0111US"
    )


def test_configured_rule_supports_bare_date_tokens(db_session):
    config = {
        "rules": {
            "DOM:PATENT": {
                "template": "{code}YYMMDD{seq:000}{country}",
                "code": "PD",
                "counter_scope": "country",
            }
        }
    }
    db_session.add(SystemConfig(key=CASE_OUR_REF_NUMBERING_CONFIG_KEY, value=json.dumps(config)))
    db_session.commit()

    assert generate_next_our_ref(division="DOM", matter_type="PATENT", country="US") == (
        f"PD{_today_yymmdd()}001US"
    )


def test_our_ref_numbering_config_validation_rejects_missing_sequence():
    validation = validate_our_ref_numbering_config_payload(
        json.dumps({"template": "AP{code}YY{country}"})
    )

    assert validation["valid"] is False
    assert any("seq" in error for error in validation["errors"])


def test_legacy_cases_next_ref_preview_does_not_consume_counter(
    admin_client, db_session, monkeypatch
):
    prefix = _dom_patent_prefix()
    existing_ref = f"{prefix}0138"
    expected_next = f"{prefix}0139"
    counter_key = f"case_ref_counter:{prefix}:US"

    db_session.add(
        Case(
            case_type="BASE",
            ref_no=existing_ref,
            title="Text Text",
            division="DOM",
            right_type="PATENT",
            country="US",
        )
    )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    monkeypatch.setattr("app.blueprints.api.routes.check_permission", lambda *args, **kwargs: True)

    for _ in range(2):
        resp = admin_client.get("/api/cases/next_ref?division=DOM&type=PATENT&country=US")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert payload.get("next_ref") == expected_next
        assert (payload.get("value") or {}).get("next_ref") == expected_next

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"


def test_legacy_cases_next_ref_out_uses_country_scoped_default_sequence(
    admin_client, db_session, monkeypatch
):
    prefix = _us_out_tm_prefix()
    counter_key = f"case_ref_counter:{prefix}:US"

    for ref, country in (
        (f"{prefix}0101", "US"),
        (f"CNFT{_yy()}0103", "CN"),
        (f"JPFT{_yy()}0106", "JP"),
    ):
        db_session.add(
            Case(
                case_type="BASE",
                ref_no=ref,
                title=f"Text Text {ref}",
                division="OUT",
                right_type="TRADEMARK",
                country=country,
            )
        )
    db_session.add(SystemConfig(key=counter_key, value="150"))
    db_session.commit()

    monkeypatch.setattr("app.blueprints.api.routes.check_permission", lambda *args, **kwargs: True)

    resp = admin_client.get("/api/cases/next_ref?division=OUT&type=TRADEMARK&country=US")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("next_ref") == f"{prefix}0102"
    assert (payload.get("value") or {}).get("next_ref") == f"{prefix}0102"

    counter = db_session.get(SystemConfig, counter_key)
    assert counter is not None
    assert counter.value == "150"
