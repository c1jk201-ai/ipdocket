from __future__ import annotations

from app.blueprints.doc import bp


def _register_existing_view_funcs() -> None:
    # Case document lists
    from app.blueprints.case.routes.list import all_letters as case_all_letters
    from app.blueprints.case.routes.list import all_notices as case_all_notices
    from app.blueprints.case.routes.list import all_responses as case_all_responses

    bp.add_url_rule("/all-notices", view_func=case_all_notices)
    bp.add_url_rule("/all-responses", view_func=case_all_responses)
    bp.add_url_rule("/all-letters", view_func=case_all_letters)


_register_existing_view_funcs()
