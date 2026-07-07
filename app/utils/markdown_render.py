from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from markupsafe import Markup, escape

_SLUG_SAFE_RE = re.compile(r"[^\w\- ]+", re.UNICODE)
_WS_RE = re.compile(r"[\s_]+", re.UNICODE)


@dataclass(frozen=True)
class RenderedMarkdown:
    html: Markup
    toc: list[dict[str, Any]]


def _slugify(raw: str, *, seen: dict[str, int]) -> str:
    s = (raw or "").strip().lower()
    s = _SLUG_SAFE_RE.sub("", s)
    s = _WS_RE.sub("-", s).strip("-")
    if not s:
        s = "section"
    n = seen.get(s, 0) + 1
    seen[s] = n
    return s if n == 1 else f"{s}-{n}"


def render_markdown_document(md_text: str) -> RenderedMarkdown:
    """
    Render a trusted Markdown document for internal help pages.
    - Disables raw HTML in Markdown.
    - Adds stable heading anchors for a TOC.
    - Adds Bootstrap table classes for readability.
    """
    try:
        from markdown_it import MarkdownIt  # type: ignore
    except Exception:
        safe = escape(md_text or "")
        html = Markup(f'<pre class="help-pre">{safe}</pre>')
        return RenderedMarkdown(html=html, toc=[])

    md = (
        MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
        .enable("table")
        .enable("strikethrough")
    )
    renderer = md.renderer

    slug_seen: dict[str, int] = {}
    toc: list[dict[str, Any]] = []

    default_heading_open = renderer.rules.get("heading_open")

    def heading_open(tokens, idx, options, env):  # type: ignore[no-untyped-def]
        token = tokens[idx]
        title = ""
        if idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
            title = tokens[idx + 1].content or ""
        slug = _slugify(title, seen=slug_seen)
        token.attrSet("id", slug)
        if token.tag in ("h2", "h3", "h4"):
            try:
                level = int(token.tag[1:])
            except Exception:
                level = 0
            toc.append({"id": slug, "title": title, "level": level})
        if callable(default_heading_open):
            return default_heading_open(tokens, idx, options, env)
        return renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["heading_open"] = heading_open

    default_table_open = renderer.rules.get("table_open")

    def table_open(tokens, idx, options, env):  # type: ignore[no-untyped-def]
        tokens[idx].attrJoin("class", "table table-sm table-bordered align-middle")
        if callable(default_table_open):
            return default_table_open(tokens, idx, options, env)
        return renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["table_open"] = table_open

    html = Markup(md.render(md_text or ""))
    return RenderedMarkdown(html=html, toc=toc)
