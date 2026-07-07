from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from flask import Flask, url_for
from markupsafe import Markup, escape

from app.core.setup.logging_setup import _log_swallowed


def register_template_filters(app: Flask) -> None:
    try:
        from flask_wtf.csrf import generate_csrf

        if "csrf_token" not in app.jinja_env.globals:
            app.jinja_env.globals["csrf_token"] = generate_csrf
        app.context_processor(lambda: {"csrf_token": generate_csrf})
    except Exception as exc:
        _log_swallowed("register_template_filters.csrf", exc)

    def _parse_option_list(raw: Optional[str]) -> list[str]:
        if not raw:
            return []
        parts = []
        for token in re.split(r"[,\n]+", raw):
            token = token.strip()
            if token:
                parts.append(token)
        return parts

    def _get_department_options() -> list[str]:
        from app.services.core.config_service import ConfigService

        raw = ConfigService.get_str("CASE_DEPARTMENT_OPTIONS", "", strip=True) or ""
        return _parse_option_list(raw)

    app.context_processor(lambda: {"department_options": _get_department_options()})

    def _get_permissions_class():
        from app.models.permissions import Permissions

        return Permissions

    app.context_processor(lambda: {"Permissions": _get_permissions_class()})

    _date_prefix_re = re.compile(r"^(?P<d>\d{4}-\d{2}-\d{2})")

    def _date_only(value):
        s = ("" if value is None else str(value)).strip()
        m = _date_prefix_re.match(s)
        return m.group("d") if m else s

    app.jinja_env.filters["date_only"] = _date_only

    def _configured_date_format() -> str:
        return app.config.get("DATE_FORMAT", "%m/%d/%Y")

    def _configured_datetime_format() -> str:
        return app.config.get("DATETIME_FORMAT", "%m/%d/%Y %I:%M:%S %p")

    def _configured_datetime_minute_format() -> str:
        return app.config.get("DATETIME_MINUTE_FORMAT", "%m/%d/%Y %I:%M %p")

    def _us_date(value, fmt: str | None = None):
        if not value:
            return ""
        out_fmt = fmt or _configured_date_format()
        try:
            if isinstance(value, datetime):
                return _local_dt(value, out_fmt)
            if isinstance(value, date):
                return value.strftime(out_fmt)
            s = str(value).strip()
            iso = _date_only(s)
            try:
                return datetime.strptime(iso[:10], "%Y-%m-%d").strftime(out_fmt)
            except ValueError:
                try:
                    return datetime.fromisoformat(s).strftime(out_fmt)
                except ValueError:
                    return s
        except Exception:
            return str(value)

    app.jinja_env.filters["us_date"] = _us_date
    app.jinja_env.filters["local_date"] = _us_date

    def _local_dt(value, fmt: str | None = None):
        if not value:
            return ""
        out_fmt = fmt or _configured_datetime_format()
        tzname = app.config.get("TIMEZONE", "America/New_York")
        try:
            if isinstance(value, date) and not isinstance(value, datetime):
                return value.strftime(out_fmt if fmt else _configured_date_format())
            dt = value
            if not isinstance(value, datetime):
                s = str(value).strip()
                try:
                    dt = datetime.fromisoformat(s)
                except ValueError:
                    try:
                        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        return s
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(ZoneInfo(tzname))
            return local.strftime(out_fmt)
        except Exception:
            return str(value)

    app.jinja_env.filters["local_dt"] = _local_dt
    app.jinja_env.filters["local_datetime"] = _local_dt

    def _local_dt_min(value):
        return _local_dt(value, _configured_datetime_minute_format())

    app.jinja_env.filters["local_dt_min"] = _local_dt_min

    app.context_processor(
        lambda: {
            "app_locale": app.config.get("LOCALE", "en-US"),
            "app_timezone": app.config.get("TIMEZONE", "America/New_York"),
            "app_date_format": app.config.get("DATE_FORMAT", "%m/%d/%Y"),
            "app_datetime_format": app.config.get("DATETIME_FORMAT", "%m/%d/%Y %I:%M:%S %p"),
        }
    )

    def _fromjson(value):
        if value is None:
            return []
        if isinstance(value, (dict, list)):
            return value
        if not value:
            return []
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []

    app.jinja_env.filters["fromjson"] = _fromjson

    def _case_field_sections(layout, fallback=None):
        fallback_sections = [] if fallback is None else fallback
        try:
            rows = list(layout or [])
        except Exception:
            return fallback_sections

        groups: dict[str, dict[str, object]] = {}
        group_order: list[str] = []
        has_explicit_group = False

        def _cell_value(cell, index: int) -> str:
            try:
                return str(cell[index] or "").strip()
            except Exception:
                return ""

        def _cell_number(cell, index: int):
            try:
                raw = cell[index]
            except Exception:
                return None
            if raw in (None, ""):
                return None
            try:
                return float(str(raw).strip())
            except Exception:
                return None

        def _append_cell(cell, side: str) -> None:
            nonlocal has_explicit_group
            key = _cell_value(cell, 1)
            if not key or key == "__blank__":
                return
            group = _cell_value(cell, 3)
            if group:
                has_explicit_group = True
            label = group or "Registry"
            if label not in groups:
                groups[label] = {
                    "left": [],
                    "right": [],
                    "order": None,
                    "first": len(group_order),
                }
                group_order.append(label)
            order_value = _cell_number(cell, 4)
            if order_value is not None:
                current_order = groups[label].get("order")
                if current_order is None or order_value < current_order:
                    groups[label]["order"] = order_value
            try:
                cell_tuple = tuple(cell)
            except Exception:
                return
            groups[label][side].append(cell_tuple)

        for row in rows:
            try:
                cells = list(row or [])
            except Exception:
                continue
            if cells:
                _append_cell(cells[0], "left")
            if len(cells) > 1:
                _append_cell(cells[1], "right")

        if not has_explicit_group:
            return fallback_sections

        blank_cell = ("", "__blank__", "blank", "", None)
        sections = []
        ordered_labels = sorted(
            group_order,
            key=lambda label: (
                groups[label].get("order")
                if groups[label].get("order") is not None
                else float(groups[label].get("first", 0)) + 10000,
                groups[label].get("first", 0),
            ),
        )
        for label in ordered_labels:
            left = groups[label]["left"]
            right = groups[label]["right"]
            pairs = []
            for index in range(max(len(left), len(right))):
                pairs.append(
                    (
                        left[index] if index < len(left) else blank_cell,
                        right[index] if index < len(right) else blank_cell,
                    )
                )
            sections.append((label, pairs))
        return sections

    app.jinja_env.filters["case_field_sections"] = _case_field_sections

    _br_re = re.compile(r"(?i)<br\s*/?>")

    def _format_status(value):
        if value is None:
            return ""
        text = str(value)
        if not text:
            return ""
        text = _br_re.sub("\n", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        safe_lines = []
        for line in str(escape(text)).split("\n"):
            if "[" in line and not line.startswith("["):
                line = line.replace("[", "<br>[")
            safe_lines.append(Markup(line))
        # Markup is safe because line content is escaped and only adds <br>.
        return Markup("<br>").join(safe_lines)

    app.jinja_env.filters["format_status"] = _format_status

    def _workflow_title(value):
        try:
            from app.utils.workflow_deadline_labels import strip_workflow_deadline_title_suffix

            return strip_workflow_deadline_title_suffix(value) or ""
        except Exception:
            return str(value or "").strip()

    app.jinja_env.filters["workflow_title"] = _workflow_title

    def _has_endpoint(endpoint: str) -> bool:
        try:
            return endpoint in app.view_functions
        except Exception:
            return False

    app.context_processor(lambda: {"has_endpoint": _has_endpoint})

    def _case_menu_sections_context() -> list[dict]:
        try:
            from app.services.case.case_menu_config import get_case_menu_config

            sections = get_case_menu_config().get("sections", [])
            out = []
            for section in sections:
                section_copy = dict(section)
                items = []
                for item in section.get("items") or []:
                    item_copy = dict(item)
                    route_params = {
                        "division": item_copy.get("division") or "",
                        "type": item_copy.get("type") or "",
                    }
                    if item_copy.get("forced_app_route"):
                        route_params["app_route"] = item_copy.get("forced_app_route") or ""
                    try:
                        item_copy["create_url"] = url_for("case_work.create_matter", **route_params)
                    except Exception:
                        item_copy["create_url"] = "#"
                    list_endpoint = item_copy.get("list_endpoint") or ""
                    try:
                        if list_endpoint and _has_endpoint(list_endpoint):
                            item_copy["list_url"] = url_for(list_endpoint)
                        else:
                            item_copy["list_url"] = url_for(
                                "case_work.list_custom_kind",
                                division_code=item_copy.get("division") or "",
                                type_code=item_copy.get("type") or "",
                            )
                    except Exception:
                        item_copy["list_url"] = item_copy["create_url"]
                    items.append(item_copy)
                section_copy["items"] = items
                out.append(section_copy)
            return out
        except Exception as exc:
            _log_swallowed("register_template_filters.case_menu_sections", exc)
            return []

    app.context_processor(lambda: {"case_menu_sections": _case_menu_sections_context()})

    _asset_version_cache: dict[tuple[str, str], tuple[float, str | None]] = {}

    def _asset_version_cache_ttl() -> float:
        try:
            return max(0.0, float(app.config.get("STATIC_ASSET_VERSION_CACHE_TTL_SECONDS") or 0))
        except Exception:
            return 0.0

    def _asset_version(static_folder: str | os.PathLike[str] | None, filename: str) -> str | None:
        if not static_folder:
            return None
        cache_key = (str(static_folder), str(filename or ""))
        ttl = _asset_version_cache_ttl()
        if ttl > 0:
            cached = _asset_version_cache.get(cache_key)
            now = time.monotonic()
            if cached and cached[0] > now:
                return cached[1]
        try:
            static_root = Path(static_folder).resolve()
            candidate = (static_root / filename).resolve()
            # Guard against path traversal and missing files.
            if not candidate.is_relative_to(static_root) or not candidate.is_file():
                if ttl > 0:
                    _asset_version_cache[cache_key] = (time.monotonic() + ttl, None)
                return None
            stat = candidate.stat()
            version = f"{int(stat.st_mtime)}-{int(stat.st_size)}"
            if ttl > 0:
                _asset_version_cache[cache_key] = (time.monotonic() + ttl, version)
            return version
        except Exception:
            if ttl > 0:
                _asset_version_cache[cache_key] = (time.monotonic() + ttl, None)
            return None

    def _asset_url(endpoint: str, filename: str) -> str:
        """Build a static URL with a deterministic cache-busting query string."""
        raw_endpoint = (endpoint or "static").strip() or "static"
        raw = (filename or "").strip()
        if not raw:
            return url_for(raw_endpoint, filename=raw)

        # Normalize separators so this also works when templates pass Windows-style paths.
        normalized = raw.replace("\\", "/").lstrip("/")
        static_folder = app.static_folder
        if raw_endpoint != "static" and raw_endpoint.endswith(".static"):
            blueprint_name = raw_endpoint.rsplit(".", 1)[0]
            blueprint = app.blueprints.get(blueprint_name)
            static_folder = getattr(blueprint, "static_folder", None) if blueprint else None

        version = _asset_version(static_folder, normalized)
        if version:
            return url_for(raw_endpoint, filename=normalized, v=version)
        return url_for(raw_endpoint, filename=normalized)

    def _static_asset_url(filename: str) -> str:
        return _asset_url("static", filename)

    def _brand_asset_url(path: str | None) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""
        lower = raw.lower()
        if lower.startswith(("http://", "https://", "data:image/")):
            return raw
        if raw.startswith("/static/"):
            return _static_asset_url(raw[len("/static/") :])
        if raw.startswith("/"):
            return raw
        return _static_asset_url(raw)

    def _branding_context():
        try:
            from app.services.core.branding import get_branding

            return get_branding()
        except Exception as exc:
            _log_swallowed("register_template_filters.branding_context", exc)
            from app.services.core.branding import Branding, DEFAULT_APP_NAME, DEFAULT_SHORT_NAME

            return Branding(
                app_name=DEFAULT_APP_NAME,
                short_name=DEFAULT_SHORT_NAME,
                logo_path="",
                favicon_path="",
                primary_color="",
                primary_rgb="",
                accent_color="",
                style="",
            )

    app.context_processor(
        lambda: {
            "asset_url": _asset_url,
            "static_asset_url": _static_asset_url,
            "brand_asset_url": _brand_asset_url,
            "branding": _branding_context(),
        }
    )
