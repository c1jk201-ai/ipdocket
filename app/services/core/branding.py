from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.core.config_service import ConfigService

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_APP_NAME = "IP Docket System"
DEFAULT_SHORT_NAME = "IP Docket"


@dataclass(frozen=True)
class Branding:
    app_name: str
    short_name: str
    logo_path: str
    favicon_path: str
    primary_color: str
    primary_rgb: str
    accent_color: str
    style: str


def _clean_text(value: str | None, default: str, *, max_len: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    return text[:max_len]


def _clean_asset_path(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith(("http://", "https://", "data:image/")):
        return text
    text = text.replace("\\", "/")
    if text.startswith("/static/"):
        return text
    if text.startswith("/"):
        return text
    return text.lstrip("/")


def _clean_hex_color(value: str | None, default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if _HEX_COLOR_RE.fullmatch(text):
        return text.lower()
    return default


def _hex_to_rgb(value: str) -> str:
    color = _clean_hex_color(value)
    if not color:
        return ""
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return f"{r}, {g}, {b}"


def get_branding() -> Branding:
    app_name = _clean_text(
        ConfigService.get_str("BRAND_APP_NAME", DEFAULT_APP_NAME, allow_blank=False),
        DEFAULT_APP_NAME,
    )
    short_name = _clean_text(
        ConfigService.get_str("BRAND_SHORT_NAME", DEFAULT_SHORT_NAME, allow_blank=False),
        DEFAULT_SHORT_NAME,
        max_len=40,
    )
    logo_path = _clean_asset_path(ConfigService.get_str("BRAND_LOGO_PATH", "", allow_blank=True))
    favicon_path = _clean_asset_path(
        ConfigService.get_str("BRAND_FAVICON_PATH", "", allow_blank=True)
    )
    primary_color = _clean_hex_color(
        ConfigService.get_str("BRAND_PRIMARY_COLOR", "", allow_blank=True)
    )
    accent_color = _clean_hex_color(
        ConfigService.get_str("BRAND_ACCENT_COLOR", "", allow_blank=True)
    )
    primary_rgb = _hex_to_rgb(primary_color)

    styles: list[str] = []
    if primary_color:
        styles.extend(
            [
                f"--app-indigo: {primary_color}",
                f"--app-indigo-2: {primary_color}",
                f"--app-navy: {primary_color}",
                f"--bs-primary: {primary_color}",
                f"--bs-link-color: {primary_color}",
                f"--bs-link-hover-color: {primary_color}",
            ]
        )
    if primary_rgb:
        styles.append(f"--bs-primary-rgb: {primary_rgb}")
    if accent_color:
        styles.extend(
            [
                f"--app-accent: {accent_color}",
                f"--bs-focus-ring-color: color-mix(in srgb, {accent_color} 35%, transparent)",
            ]
        )

    return Branding(
        app_name=app_name,
        short_name=short_name,
        logo_path=logo_path,
        favicon_path=favicon_path,
        primary_color=primary_color,
        primary_rgb=primary_rgb,
        accent_color=accent_color,
        style="; ".join(styles),
    )
