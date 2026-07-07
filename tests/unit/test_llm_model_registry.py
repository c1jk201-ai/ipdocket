from __future__ import annotations

import json
from types import SimpleNamespace

from app.models.system_config import SystemConfig
from app.services.core.config_service import ConfigService


def _set_config(db_session, key: str, value: str) -> None:
    row = SystemConfig.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db_session.add(SystemConfig(key=key, value=str(value)))
    db_session.commit()
    ConfigService.clear_cache()


def test_llm_model_registry_uses_single_default_model(app, db_session, monkeypatch):
    from app.services.core.llm_model_registry import describe_llm_model_settings, resolve_llm_model

    monkeypatch.delenv("BILLING_INVOICE_LLM_MODEL", raising=False)
    monkeypatch.delenv("CLIENT_TAGGING_LLM_MODEL", raising=False)
    monkeypatch.delenv("FOREIGN_EMAIL_LLM_MODEL", raising=False)
    monkeypatch.delenv("NOTICE_DUE_POLICY_LLM_MODEL", raising=False)
    app.config.pop("BILLING_INVOICE_LLM_MODEL", None)
    app.config.pop("CLIENT_TAGGING_LLM_MODEL", None)
    app.config.pop("FOREIGN_EMAIL_LLM_MODEL", None)
    app.config.pop("NOTICE_DUE_POLICY_LLM_MODEL", None)
    ConfigService.clear_cache()

    _set_config(db_session, "LLM_DEFAULT_MODEL", "gpt-default-test")
    _set_config(db_session, "BILLING_INVOICE_LLM_MODEL", "gpt-billing-test")
    _set_config(db_session, "CLIENT_TAGGING_LLM_MODEL", "gpt-client-test")
    _set_config(db_session, "FOREIGN_EMAIL_LLM_MODEL", "gpt-foreign-test")
    _set_config(db_session, "NOTICE_DUE_POLICY_LLM_MODEL", "gpt-notice-test")

    assert resolve_llm_model("default") == "gpt-default-test"
    assert resolve_llm_model("billing_invoice") == "gpt-default-test"
    assert resolve_llm_model("client_tagging") == "gpt-default-test"
    assert resolve_llm_model("foreign_email") == "gpt-default-test"
    assert resolve_llm_model("notice_due_policy") == "gpt-default-test"

    rows = {row["slug"]: row for row in describe_llm_model_settings()}
    assert set(rows) == {"default"}
    assert rows["default"]["effective_value"] == "gpt-default-test"
    assert rows["default"]["resolved_from_key"] == "LLM_DEFAULT_MODEL"
    assert rows["default"]["effective_source"] == "system_config"


def test_client_tagging_uses_default_model_by_default(app, db_session, monkeypatch):
    from app.services.client import client_tagging

    _set_config(db_session, "LLM_DEFAULT_MODEL", "gpt-default-tags-test")
    _set_config(db_session, "CLIENT_TAGGING_LLM_MODEL", "gpt-client-tags-test")
    client_tagging._LLM_TAGS_CACHE.clear()

    captured: dict[str, str] = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            captured["model"] = kwargs["model"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"tags":["Text Text Text","Text"]}'
                        )
                    )
                ]
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            captured["api_key"] = api_key
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr(client_tagging, "OpenAI", _FakeOpenAI)

    tags = client_tagging._generate_company_name_tags_llm("Example IP Law Office", "sk-test")

    assert captured["api_key"] == "sk-test"
    assert captured["model"] == "gpt-default-tags-test"
    assert "Text Text Text" in tags


def test_oa_citation_uses_default_model(app, db_session, monkeypatch):
    import app.services.citations.cited_reference_service as citation_service

    _set_config(db_session, "LLM_DEFAULT_MODEL", "gpt-default-citation-test")
    _set_config(db_session, "OA_CITATION_AI_MODEL", "gpt-oa-legacy-test")

    captured: dict[str, str] = {}
    payload = {
        "references": [
            {
                "label": "Reference 1",
                "ref_type": "patent",
                "country": "US",
                "publication_number": "US2019/0373976",
                "published_date": "2019-12-12",
                "title": "",
                "raw_text": "US2019/0373976",
            }
        ]
    }

    class _FakeCompletions:
        def create(self, **kwargs):
            captured["model"] = kwargs["model"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
                    )
                ]
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            captured["api_key"] = api_key
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr(citation_service, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(citation_service, "get_openai_api_key", lambda: "sk-test")

    refs = citation_service.parse_ai_citations_from_text("OA text with US2019/0373976")

    assert captured["api_key"] == "sk-test"
    assert captured["model"] == "gpt-default-citation-test"
    assert refs[0].publication_number == "US2019/0373976"


def test_llm_runtime_reads_system_config_openai_key(app, db_session, monkeypatch):
    from app.services.core.llm_runtime import get_openai_api_key

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ASSISTANT_OPENAI_API_KEY", raising=False)
    app.config.pop("OPENAI_API_KEY", None)
    app.config.pop("ASSISTANT_OPENAI_API_KEY", None)
    ConfigService.clear_cache()

    _set_config(db_session, "OPENAI_API_KEY", "sk-system-primary")
    assert get_openai_api_key() == "sk-system-primary"


def test_llm_runtime_legacy_key_is_optional(app, db_session, monkeypatch):
    from app.services.core.llm_runtime import get_openai_api_key

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ASSISTANT_OPENAI_API_KEY", raising=False)
    app.config.pop("OPENAI_API_KEY", None)
    app.config.pop("ASSISTANT_OPENAI_API_KEY", None)
    ConfigService.clear_cache()

    _set_config(db_session, "ASSISTANT_OPENAI_API_KEY", "sk-legacy-only")
    assert get_openai_api_key() == "sk-legacy-only"
    assert get_openai_api_key(allow_legacy=False) == ""


def test_llm_runtime_uses_single_input_limit_config_key(app, db_session):
    from app.services.core.llm_runtime import (
        DEFAULT_LLM_INPUT_MAX_CHARS,
        get_llm_input_max_chars,
    )

    _set_config(db_session, "INVOICE_LLM_MAX_CHARS", "7")
    assert get_llm_input_max_chars() == DEFAULT_LLM_INPUT_MAX_CHARS

    _set_config(db_session, "LLM_INPUT_MAX_CHARS", "11")
    assert get_llm_input_max_chars() == 11


def test_admin_config_masks_sensitive_values(admin_client, db_session):
    _set_config(db_session, "OPENAI_API_KEY", "sk-secret-visible-no-more")

    response = admin_client.get("/admin/config")
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert "sk-secret-visible-no-more" not in html
    assert "***" in html
