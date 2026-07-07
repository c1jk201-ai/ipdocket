from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path

from flask import current_app, render_template
from flask_login import login_required

from app.blueprints.help import bp
from app.utils.markdown_render import render_markdown_document

_MIN_HELP_DOCUMENT_BYTES = 4_096
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UPDATED_RE = re.compile(r" :\s*\*\*(\d{4}-\d{2}-\d{2})\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CODE_RE = re.compile(r"`([^`]+)`")
_TABLE_RE = re.compile(r"^\|.*\|$")
_LIST_RE = re.compile(r"^(?:[-*]|\d+\.)\s+")
_SPOTLIGHT_SECTION_TITLES = [
    ("Quick Start", "bi-rocket-takeoff"),
    ("Search", "bi-search"),
    ("Uploads", "bi-cloud-arrow-up"),
    ("Billing(Invoice System)", "bi-receipt"),
    ("External AI", "bi-stars"),
]


def _project_root() -> Path:
    # app/ is a package; project root is one level up.
    return Path(current_app.root_path).resolve().parent


def _help_doc_candidates(project_root: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []

    # 1) Optional override (absolute or project-relative)
    cfg_path = (current_app.config.get("HELP_MANUAL_PATH") or "").strip()
    if cfg_path:
        p = Path(cfg_path)
        if not p.is_absolute():
            p = project_root / p
        candidates.append((p, cfg_path))

    return candidates


def _is_valid_help_document(raw: str) -> bool:
    return bool((raw or "").strip()) and len(raw) >= _MIN_HELP_DOCUMENT_BYTES


def _builtin_help_markdown() -> str:
    # Last-resort help content when external markdown files are unavailable.
    return (
        "# IPM Help\n\n"
        "## Quick Start\n"
        "- Use the navigation menu to open matters, deadlines, invoices, CRM, and settings.\n"
        "- Use search to find matters, clients, invoices, and documents.\n"
        "- Save changes on detail pages before leaving the page.\n\n"
        "## Access\n"
        "- Sign in with a local account configured by an administrator.\n"
        "- Role and permission changes are managed from the admin screens.\n"
        "- Contact an administrator if a page or action is unavailable.\n\n"
        "## Uploads\n"
        "- Upload only files that are required for the selected matter or workflow.\n"
        "- Large files, archives, and parsed documents are subject to configured safety limits.\n"
        "- Keep production upload and backup directories outside the source tree.\n\n"
        "## Operations\n"
        "- Configure secrets and database settings through environment variables.\n"
        "- Use a shared rate-limit backend in production.\n"
        "- Keep runtime schema creation disabled in production deployments.\n"
    )


def _strip_inline_markdown(text: str) -> str:
    cleaned = _LINK_RE.sub(r"\1", text or "")
    cleaned = _CODE_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = cleaned.replace("*", "").replace("_", "").replace("~", "")
    return " ".join(cleaned.split()).strip()


def _iter_markdown_headings(raw: str) -> list[dict[str, int | str]]:
    headings: list[dict[str, int | str]] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        headings.append(
            {
                "line_no": line_no,
                "level": len(match.group(1)),
                "title": _strip_inline_markdown(match.group(2)),
            }
        )
    return headings


def _extract_section_summary(lines: list[str]) -> str:
    block: list[str] = []
    mode = ""

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if block:
                break
            continue
        if line.startswith("#") or line == "---" or _TABLE_RE.match(line):
            if block:
                break
            continue
        if line.startswith(">"):
            continue
        if not block and line.endswith(":") and len(line) <= 24:
            continue

        list_match = _LIST_RE.match(line)
        if list_match:
            if block and mode == "paragraph":
                break
            mode = "list"
            block.append(_strip_inline_markdown(line[list_match.end() :]))
            if len(block) >= 2:
                break
            continue

        if block and mode == "list":
            break

        mode = "paragraph"
        block.append(_strip_inline_markdown(line))
        if len(" ".join(block)) >= 180:
            break

    if not block:
        return ""
    if mode == "list":
        return " / ".join(block[:2])
    return " ".join(block)


def _build_toc_groups(toc: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    current_group: dict[str, object] | None = None

    for item in toc:
        title = _strip_inline_markdown(str(item.get("title") or ""))
        entry = {
            "id": str(item.get("id") or ""),
            "title": title,
            "level": int(item.get("level") or 0),
            "filter_text": title.lower(),
        }
        if entry["level"] == 2:
            current_group = {
                "id": entry["id"],
                "title": entry["title"],
                "items": [],
                "filter_text": title.lower(),
            }
            groups.append(current_group)
            continue
        if current_group is None:
            continue
        current_group["items"].append(entry)
        current_group["filter_text"] = (
            f"{current_group['filter_text']} {entry['title'].lower()}".strip()
        )

    return groups


def _build_help_page_data(
    raw: str, toc: list[dict[str, object]], doc_bytes: int
) -> dict[str, object]:
    lines = (raw or "").splitlines()
    headings = _iter_markdown_headings(raw)
    toc_ids_by_title: dict[str, deque[str]] = defaultdict(deque)
    for item in toc:
        title = _strip_inline_markdown(str(item.get("title") or ""))
        toc_ids_by_title[title].append(str(item.get("id") or ""))

    top_sections = [heading for heading in headings if heading["level"] == 2]
    sections: list[dict[str, object]] = []

    for index, heading in enumerate(top_sections):
        title = str(heading["title"])
        start_line = int(heading["line_no"])
        next_start_line = (
            int(top_sections[index + 1]["line_no"])
            if index + 1 < len(top_sections)
            else len(lines) + 1
        )
        section_lines = lines[start_line : next_start_line - 1]
        section_id = (
            toc_ids_by_title[title].popleft() if toc_ids_by_title[title] else f"section-{index + 1}"
        )
        subsection_titles = [
            str(child["title"])
            for child in headings
            if child["level"] == 3 and start_line < int(child["line_no"]) < next_start_line
        ]
        summary = _extract_section_summary(section_lines)
        filter_parts = [title, summary, *subsection_titles[:4]]
        sections.append(
            {
                "id": section_id,
                "title": title,
                "summary": summary,
                "subsections": subsection_titles,
                "subsection_count": len(subsection_titles),
                "filter_text": " ".join(part for part in filter_parts if part).lower(),
            }
        )

    section_map = {str(section["title"]): section for section in sections}
    spotlight_sections = []
    for title, icon in _SPOTLIGHT_SECTION_TITLES:
        section = section_map.get(title)
        if not section:
            continue
        spotlight_sections.append(
            {
                "id": section["id"],
                "title": section["title"],
                "summary": section["summary"],
                "subsections": list(section["subsections"])[:3],
                "icon": icon,
                "filter_text": section["filter_text"],
            }
        )

    quick_start_section = section_map.get("Quick Start")
    quick_links = []
    if quick_start_section:
        toc_links_by_title: dict[str, deque[str]] = defaultdict(deque)
        for item in toc:
            title = _strip_inline_markdown(str(item.get("title") or ""))
            toc_links_by_title[title].append(str(item.get("id") or ""))
        for title in list(quick_start_section["subsections"])[:5]:
            quick_links.append(
                {
                    "title": title,
                    "id": toc_links_by_title[title].popleft() if toc_links_by_title[title] else "",
                }
            )

    updated_at_match = _UPDATED_RE.search(raw or "")
    updated_at = updated_at_match.group(1) if updated_at_match else ""
    hero_summary = ""
    if section_map.get("Current Document"):
        hero_summary = str(section_map["Current Document"]["summary"])
    elif sections:
        hero_summary = str(sections[0]["summary"])

    kb = max(1, round(doc_bytes / 1024)) if doc_bytes else 0

    return {
        "hero_summary": hero_summary,
        "updated_at": updated_at,
        "sections": sections,
        "spotlight_sections": spotlight_sections,
        "quick_links": quick_links,
        "toc_groups": _build_toc_groups(toc),
        "stats": [
            {"label": "Updated", "value": updated_at or "Internal document"},
            {"label": "Sections", "value": str(len(sections))},
            {"label": "Anchors", "value": str(len(toc))},
            {"label": "Size", "value": f"{kb}KB"},
        ],
    }


@bp.get("/")
@login_required
def index():
    project_root = _project_root()
    raw = ""
    doc_label = "embedded://help-manual"
    doc_bytes = 0

    for path, label in _help_doc_candidates(project_root):
        try:
            if not path.exists() or not path.is_file():
                continue
            raw_candidate = path.read_text(encoding="utf-8")
            if not _is_valid_help_document(raw_candidate):
                current_app.logger.warning(
                    "Help manual candidate skipped: content is too short.",
                    extra={"candidate_path": str(path), "candidate_bytes": len(raw_candidate)},
                )
                continue
            raw = raw_candidate
            doc_label = label
            doc_bytes = len(raw)
            break
        except Exception:
            continue

    if not raw:
        candidates = _help_doc_candidates(project_root)
        if candidates:
            current_app.logger.warning(
                "Help manual could not be loaded from any known path.",
                extra={"candidate_paths": [str(p) for p, _ in candidates]},
            )
        raw = _builtin_help_markdown()
        doc_bytes = len(raw)
        doc_label = "embedded://help-manual"

    rendered = render_markdown_document(raw)
    page_data = _build_help_page_data(raw, rendered.toc, doc_bytes)
    return render_template(
        "help/index.html",
        title="Help",
        doc_title="IPM Help",
        doc_path=doc_label,
        doc_bytes=doc_bytes,
        toc=rendered.toc,
        content_html=rendered.html,
        hero_summary=page_data["hero_summary"],
        help_sections=page_data["sections"],
        spotlight_sections=page_data["spotlight_sections"],
        quick_links=page_data["quick_links"],
        toc_groups=page_data["toc_groups"],
        page_stats=page_data["stats"],
        doc_updated_at=page_data["updated_at"],
    )
