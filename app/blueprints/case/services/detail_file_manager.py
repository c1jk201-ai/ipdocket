from __future__ import annotations

from flask import current_app
from sqlalchemy import desc, or_

from app.extensions import db
from app.models.ip_records import FileAsset, MatterFileAsset
from app.utils.coercion import coerce_int


def _detail_int_cfg(key: str, default: int, *, min_v: int = 1, max_v: int = 5000) -> int:
    raw = current_app.config.get(key, default)
    val = coerce_int(raw, default) or int(default)
    return max(min_v, min(max_v, val))


def build_file_manager_section(
    ctx: dict,
    request_args: dict,
    *,
    counts_only: bool = False,
) -> dict:
    mid_str = ctx["_mid_str"]
    fm_folder_id = request_args.get("fm_folder_id")

    def _int_arg(key: str, default: int) -> int:
        try:
            raw = (request_args.get(key) or "").strip()
            if not raw:
                return int(default)
            return int(raw)
        except Exception:
            return int(default)

    fm_page = max(1, _int_arg("fm_page", 1))
    fm_per_page_default = _detail_int_cfg("CASE_DETAIL_FM_PER_PAGE", 200, min_v=20, max_v=2000)
    fm_max_per_page = _detail_int_cfg("CASE_DETAIL_FM_MAX_PER_PAGE", 500, min_v=20, max_v=5000)
    fm_per_page = _int_arg("fm_per_page", fm_per_page_default)
    fm_per_page = max(1, min(fm_max_per_page, fm_per_page))
    fm_offset = (fm_page - 1) * fm_per_page

    system_roles = ["email", "notice", "letter", "application", "response", "uspto", "opinion"]
    base_role_filter = or_(
        MatterFileAsset.role.notin_(system_roles),
        MatterFileAsset.role.is_(None),
    )

    fm_total_query = (
        db.session.query(MatterFileAsset)
        .filter(MatterFileAsset.matter_id == mid_str)
        .filter(base_role_filter)
    )
    fm_total_count = fm_total_query.count()

    if counts_only:
        return {
            "fm_total_count": fm_total_count,
            "fm_files": [],
            "fm_folder_id": fm_folder_id,
            "current_folder": None,
            "fm_page": fm_page,
            "fm_per_page": fm_per_page,
            "fm_has_next": False,
            "fm_has_prev": False,
            "fm_current_total_count": 0,
        }

    query = (
        db.session.query(MatterFileAsset, FileAsset)
        .join(FileAsset, MatterFileAsset.file_asset_id == FileAsset.file_asset_id)
        .filter(MatterFileAsset.matter_id == mid_str)
        .filter(base_role_filter)
    )

    current_folder = None
    if fm_folder_id:
        query = query.filter(MatterFileAsset.parent_id == fm_folder_id)
        current_folder = MatterFileAsset.query.filter_by(matter_file_id=fm_folder_id).first()
    else:
        query = query.filter(
            or_(MatterFileAsset.parent_id.is_(None), MatterFileAsset.parent_id == "")
        )

    fm_current_total_count = query.count()
    if fm_current_total_count and fm_offset >= fm_current_total_count:
        fm_page = max(1, (fm_current_total_count + fm_per_page - 1) // fm_per_page)
        fm_offset = (fm_page - 1) * fm_per_page

    fm_files = (
        query.order_by(desc(FileAsset.created_at)).limit(fm_per_page + 1).offset(fm_offset).all()
    )
    fm_has_next = bool(len(fm_files) > fm_per_page)
    if fm_has_next:
        fm_files = fm_files[:fm_per_page]
    fm_has_prev = fm_page > 1

    return {
        "fm_total_count": fm_total_count,
        "fm_files": fm_files,
        "fm_folder_id": fm_folder_id,
        "current_folder": current_folder,
        "fm_page": fm_page,
        "fm_per_page": fm_per_page,
        "fm_has_next": fm_has_next,
        "fm_has_prev": fm_has_prev,
        "fm_current_total_count": fm_current_total_count,
    }
