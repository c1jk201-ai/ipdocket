from __future__ import annotations

from pathlib import Path

_BANNED_ASSET_CDN_PATTERNS = (
    "cdn.jsdelivr.net/",
    "unpkg.com/",
    "cdnjs.cloudflare.com/",
)

_REQUIRED_VENDOR_FILES = (
    "app/static/vendor/bootstrap/css/bootstrap.min.css",
    "app/static/vendor/bootstrap/js/bootstrap.bundle.min.js",
    "app/static/vendor/bootstrap-icons/font/bootstrap-icons.css",
    "app/static/vendor/bootstrap-icons/font/fonts/bootstrap-icons.woff",
    "app/static/vendor/bootstrap-icons/font/fonts/bootstrap-icons.woff2",
    "app/static/vendor/chart/chart.umd.min.js",
    "app/static/vendor/flatpickr/flatpickr.min.css",
    "app/static/vendor/flatpickr/flatpickr.min.js",
    "app/static/vendor/fullcalendar/index.global.min.js",
    "app/static/vendor/htmx/htmx.min.js",
    "app/static/vendor/pdfjs/pdf.min.js",
    "app/static/vendor/pdfjs/pdf.worker.min.js",
)

_REQUIRED_NOTICE_TERMS = (
    "Bootstrap",
    "5.3.0",
    "app/static/vendor/bootstrap/css/bootstrap.min.css",
    "Bootstrap Icons",
    "1.11.1",
    "app/static/vendor/bootstrap-icons/font/bootstrap-icons.css",
    "Chart.js",
    "4.4.0",
    "app/static/vendor/chart/chart.umd.min.js",
    "flatpickr",
    "4.6.13",
    "app/static/vendor/flatpickr/flatpickr.min.js",
    "FullCalendar",
    "6.1.9",
    "app/static/vendor/fullcalendar/index.global.min.js",
    "htmx",
    "1.9.10",
    "BSD-2-Clause",
    "app/static/vendor/htmx/htmx.min.js",
    "PDF.js",
    "3.11.174",
    "Apache-2.0",
    "app/static/vendor/pdfjs/pdf.worker.min.js",
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def test_templates_do_not_depend_on_external_asset_cdns() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []

    for path in sorted((root / "app" / "templates").rglob("*.html")):
        text = _read_text(path)
        hits = [pattern for pattern in _BANNED_ASSET_CDN_PATTERNS if pattern in text]
        if hits:
            rel_path = path.relative_to(root)
            offenders.append(f"{rel_path}: {', '.join(hits)}")

    assert not offenders, "External asset CDN references remain:\n" + "\n".join(offenders)


def test_required_local_vendor_assets_exist() -> None:
    root = Path(__file__).resolve().parents[2]
    missing = [rel_path for rel_path in _REQUIRED_VENDOR_FILES if not (root / rel_path).is_file()]
    assert not missing, "Missing local vendor assets:\n" + "\n".join(missing)


def test_required_local_vendor_assets_have_notices() -> None:
    root = Path(__file__).resolve().parents[2]
    notice_path = root / "docs" / "THIRD_PARTY_NOTICES.md"

    assert notice_path.is_file(), "Missing docs/THIRD_PARTY_NOTICES.md"

    notice = _read_text(notice_path)
    missing = [term for term in _REQUIRED_NOTICE_TERMS if term not in notice]
    assert not missing, "Third-party notice is missing required terms:\n" + "\n".join(missing)
