from __future__ import annotations


def test_build_client_search_tags_includes_original_name():
    from app.services.client.client_tagging import build_client_search_tags

    tags = build_client_search_tags(["Text"])
    assert "Text" in tags


def test_build_client_search_tags_includes_ascii_acronym():
    from app.services.client.client_tagging import build_client_search_tags

    tags = build_client_search_tags(["Example IP Law Office"])
    assert "EILO" in tags
    assert "eilo" in tags


def test_build_client_search_tags_llm_adds_aliases(monkeypatch):
    from app.services.client import client_tagging

    calls: list[str] = []

    def fake_llm(name: str, api_key: str, *, model: str = "gpt-4o-mini") -> list[str]:
        calls.append(name)
        return ["Example IP", "example law"]

    monkeypatch.setattr(client_tagging, "_generate_company_name_tags_llm", fake_llm)

    tags = client_tagging.build_client_search_tags(
        ["Example IP Law Office"], api_key="sk-test", use_llm=True
    )
    assert calls == ["Example IP Law Office"]
    assert "example law" in tags


def test_build_client_search_tags_llm_runs_for_configured_input(monkeypatch):
    from app.services.client import client_tagging

    calls: list[str] = []

    def fake_llm(name: str, api_key: str, *, model: str = "gpt-4o-mini") -> list[str]:
        calls.append(name)
        return ["example ip law office"]

    monkeypatch.setattr(client_tagging, "_generate_company_name_tags_llm", fake_llm)

    tags = client_tagging.build_client_search_tags(["Text"], api_key="sk-test", use_llm=True)
    assert calls == ["Text"]
    assert "example ip law office" in tags
