from __future__ import annotations

from types import SimpleNamespace

from app.blueprints.case.routes.general_edit import _resolve_active_edit_namespace


def test_resolve_active_edit_namespace_prefers_profile_namespace() -> None:
    resolution = _resolve_active_edit_namespace(
        resolved_profile=SimpleNamespace(namespace="incoming_design"),
        div="DOM",
        typ="PATENT",
        custom_rows={"pct": object(), "incoming_trademark": object()},
    )

    assert resolution.active_namespace == "incoming_design"
    assert resolution.fallback_candidates == ()


def test_resolve_active_edit_namespace_uses_single_fallback_namespace() -> None:
    resolution = _resolve_active_edit_namespace(
        resolved_profile=None,
        div="ETC",
        typ="UNKNOWN",
        custom_rows={"pct": object()},
    )

    assert resolution.active_namespace == "pct"
    assert resolution.fallback_candidates == ("pct",)


def test_resolve_active_edit_namespace_rejects_ambiguous_fallback_namespaces() -> None:
    resolution = _resolve_active_edit_namespace(
        resolved_profile=None,
        div="ETC",
        typ="UNKNOWN",
        custom_rows={"pct": object(), "misc": object()},
    )

    assert resolution.active_namespace is None
    assert resolution.fallback_candidates == ("pct", "misc")
