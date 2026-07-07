from __future__ import annotations

from typing import Any, Callable


def resolve_case_profile_syncs(
    *,
    mapping_division: str,
    mapping_type: str,
    division: str,
    case_type: str,
) -> tuple[Callable[..., Any] | None, Callable[..., Any] | None]:
    from app.blueprints.case.helpers import (
        _sync_matter_events_from_dom_design,
        _sync_matter_events_from_dom_patent,
        _sync_matter_events_from_dom_trademark,
        _sync_matter_events_from_inc_design,
        _sync_matter_events_from_inc_patent,
        _sync_matter_events_from_inc_trademark,
        _sync_matter_events_from_litigation,
        _sync_matter_events_from_out_design,
        _sync_matter_events_from_out_patent,
        _sync_matter_events_from_out_trademark,
        _sync_matter_events_from_pct,
        _sync_matter_identifiers_from_dom_design,
        _sync_matter_identifiers_from_dom_patent,
        _sync_matter_identifiers_from_dom_trademark,
        _sync_matter_identifiers_from_inc_design,
        _sync_matter_identifiers_from_inc_patent,
        _sync_matter_identifiers_from_inc_trademark,
        _sync_matter_identifiers_from_out_design,
        _sync_matter_identifiers_from_out_patent,
        _sync_matter_identifiers_from_out_trademark,
        _sync_matter_identifiers_from_pct,
    )

    if mapping_type == "LITIGATION":
        return None, _sync_matter_events_from_litigation
    if mapping_type == "MISC":
        return None, None
    if mapping_type == "PCT":
        return _sync_matter_identifiers_from_pct, _sync_matter_events_from_pct

    prefix = {"DOM": "dom", "INC": "inc", "OUT": "out"}.get(mapping_division)
    if not prefix:
        raise ValueError(f"Unsupported case profile: division={division!r}, type={case_type!r}")

    suffix = f"{prefix}_{mapping_type.lower()}"
    id_syncs = {
        "dom_patent": _sync_matter_identifiers_from_dom_patent,
        "dom_design": _sync_matter_identifiers_from_dom_design,
        "dom_trademark": _sync_matter_identifiers_from_dom_trademark,
        "inc_patent": _sync_matter_identifiers_from_inc_patent,
        "inc_design": _sync_matter_identifiers_from_inc_design,
        "inc_trademark": _sync_matter_identifiers_from_inc_trademark,
        "out_patent": _sync_matter_identifiers_from_out_patent,
        "out_design": _sync_matter_identifiers_from_out_design,
        "out_trademark": _sync_matter_identifiers_from_out_trademark,
    }
    event_syncs = {
        "dom_patent": _sync_matter_events_from_dom_patent,
        "dom_design": _sync_matter_events_from_dom_design,
        "dom_trademark": _sync_matter_events_from_dom_trademark,
        "inc_patent": _sync_matter_events_from_inc_patent,
        "inc_design": _sync_matter_events_from_inc_design,
        "inc_trademark": _sync_matter_events_from_inc_trademark,
        "out_patent": _sync_matter_events_from_out_patent,
        "out_design": _sync_matter_events_from_out_design,
        "out_trademark": _sync_matter_events_from_out_trademark,
    }
    id_sync = id_syncs.get(suffix)
    event_sync = event_syncs.get(suffix)
    if not id_sync or not event_sync:
        raise ValueError(f"Unsupported case profile: division={division!r}, type={case_type!r}")
    return id_sync, event_sync
