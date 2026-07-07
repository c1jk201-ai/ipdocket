from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

_GLOBAL_ALLOWED_ATTRS = {"colspan", "rowspan", "align"}
_ALLOWED_ATTRS_BY_TAG = {
    "a": {"href", "title", "target", "rel"},
}
_ALLOWED_LINK_SCHEMES = {"http", "https", "mailto", "tel"}
_BLOCKED_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "link",
    "meta",
    "base",
    "img",
    "svg",
}


def _sanitize_href(raw: str | None) -> str | None:
    if raw is None:
        return None
    href = str(raw).strip()
    if not href:
        return None
    if any(ch in href for ch in ("\x00", "\r", "\n", "\t")):
        return None
    parsed = urlparse(href)
    if parsed.scheme:
        if parsed.scheme.lower() not in _ALLOWED_LINK_SCHEMES:
            return None
    else:
        if href.startswith("//"):
            return None
    return href


class _EmailHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = (tag or "").lower()
        if tag in _BLOCKED_TAGS:
            self._skip_tag = tag
            return
        if self._skip_tag:
            return
        if tag not in _ALLOWED_TAGS:
            return
        rendered_attrs = self._render_attrs(tag, attrs)
        if rendered_attrs:
            self._parts.append(f"<{tag} {rendered_attrs}>")
        else:
            self._parts.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = (tag or "").lower()
        if tag in _BLOCKED_TAGS:
            return
        if self._skip_tag:
            return
        if tag not in _ALLOWED_TAGS:
            return
        rendered_attrs = self._render_attrs(tag, attrs)
        if rendered_attrs:
            self._parts.append(f"<{tag} {rendered_attrs} />")
        else:
            self._parts.append(f"<{tag} />")

    def handle_endtag(self, tag: str) -> None:
        tag = (tag or "").lower()
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_tag = None
            return
        if tag in _ALLOWED_TAGS:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_tag:
            return
        self._parts.append(escape(data))

    def handle_entityref(self, name: str) -> None:
        if self._skip_tag:
            return
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_tag:
            return
        self._parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return

    def _render_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed_attrs = set(_GLOBAL_ALLOWED_ATTRS)
        allowed_attrs.update(_ALLOWED_ATTRS_BY_TAG.get(tag, set()))

        safe_attrs: dict[str, str] = {}
        for key, val in attrs or []:
            key = (key or "").lower()
            if key not in allowed_attrs or val is None:
                continue
            if tag == "a" and key == "href":
                safe_href = _sanitize_href(val)
                if not safe_href:
                    continue
                val = safe_href
            if tag == "a" and key == "target":
                val = str(val).strip()
                if not val:
                    continue
            safe_attrs[key] = str(val)

        if tag == "a" and safe_attrs.get("target") == "_blank":
            rel_value = safe_attrs.get("rel", "")
            rel_tokens = []
            seen = set()
            for token in rel_value.split():
                token_norm = token.lower()
                if token_norm not in seen:
                    seen.add(token_norm)
                    rel_tokens.append(token)
            for token in ("noopener", "noreferrer"):
                if token not in seen:
                    rel_tokens.append(token)
            if rel_tokens:
                safe_attrs["rel"] = " ".join(rel_tokens)

        rendered = []
        for key, val in safe_attrs.items():
            rendered.append(f'{key}="{escape(val, quote=True)}"')
        return " ".join(rendered)

    def sanitized(self) -> str:
        return "".join(self._parts)


def sanitize_email_html(raw: str | None) -> str:
    if not raw:
        return ""
    parser = _EmailHtmlSanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return escape(raw)
    return parser.sanitized()
