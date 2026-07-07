from __future__ import annotations

import bisect
import ipaddress
import logging
import os
import re
from functools import lru_cache
from typing import Iterable

from flask import current_app, request
from flask_login import current_user

from app.services.core.config_service import ConfigService

logger = logging.getLogger(__name__)

_DEFAULT_INTERNAL_CIDRS = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

_DEFAULT_COUNTRY_CIDR_DIR = "/app/data/country_cidrs"


def _get_setting(key: str, default=None):
    return ConfigService.get_raw(key, default)


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _split_tokens(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t for t in re.split(r"[,\s]+", str(raw)) if t]


def _parse_networks(tokens: Iterable[str]) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for token in tokens:
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return networks


def _strip_zone(ip_raw: str) -> str:
    return ip_raw.split("%", 1)[0] if ip_raw else ip_raw


def get_client_ip(req=None) -> str | None:
    req = req or request
    # ProxyFix normalizes remote_addr only for trusted proxy peers. Reading
    # X-Forwarded-For here would reintroduce client-controlled spoofing.
    ip = (req.remote_addr or "").strip()
    return _strip_zone(ip) if ip else None


@lru_cache(maxsize=1)
def _internal_networks_cached(raw: str | None) -> list[ipaddress._BaseNetwork]:
    tokens = list(_DEFAULT_INTERNAL_CIDRS) + _split_tokens(raw)
    return _parse_networks(tokens)


def is_internal_request(req=None) -> bool:
    req = req or request
    trust_proxy = _to_bool(_get_setting("SECURITY_TRUST_PROXY_HEADERS", False))
    if not trust_proxy:
        # Guardrail: if an upstream proxy adds forwarding headers but we are not configured to
        # trust them, do not treat the proxy's private `remote_addr` as "internal".
        # This prevents accidental bypasses (e.g., country block) under misconfiguration.
        try:
            if req.headers.get("X-Forwarded-For") or req.headers.get("X-Real-IP"):
                return False
        except Exception:
            return False

    ip = get_client_ip(req)
    if not ip:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    raw = _get_setting("SECURITY_INTERNAL_CIDRS", "")
    networks = _internal_networks_cached(str(raw) if raw is not None else "")
    return any(ip_obj in net for net in networks)


def _blocked_country_codes() -> set[str]:
    raw = _get_setting("SECURITY_BLOCKED_COUNTRY_CODES", "")
    tokens = _split_tokens(raw)
    return {t.upper() for t in tokens if t}


@lru_cache(maxsize=1)
def _geoip_reader(path: str):
    from geoip2.database import Reader

    return Reader(path)


def _lookup_country_code(ip: str) -> str | None:
    path = _get_setting("SECURITY_GEOIP_COUNTRY_DB", "")
    if not path:
        return None
    try:
        reader = _geoip_reader(str(path))
    except Exception:
        return None
    try:
        result = reader.country(ip)
        code = result.country.iso_code
        return code.upper() if code else None
    except Exception:
        return None


def _country_cidr_dir() -> str:
    raw = _get_setting("SECURITY_COUNTRY_CIDR_DIR", "")
    try:
        raw = str(raw or "").strip()
    except Exception:
        raw = ""
    return raw or _DEFAULT_COUNTRY_CIDR_DIR


def _zone_file_path(country_code: str) -> str:
    # Convention: <dir>/<lowercase ISO2>.zone, each line is an IPv4 CIDR (e.g., 1.0.1.0/24)
    cc = (country_code or "").strip().lower()
    return os.path.join(_country_cidr_dir(), f"{cc}.zone")


def _file_mtime(path: str) -> int:
    try:
        return int(os.stat(path).st_mtime)
    except Exception:
        return 0


# starts, ends (same length, sorted by starts, merged non-overlapping)
_IPv4RangeIndex = tuple[tuple[int, ...], tuple[int, ...]]


@lru_cache(maxsize=256)
def _load_ipv4_range_index_from_zone_file(path: str, mtime: int) -> _IPv4RangeIndex:
    # `mtime` is part of the cache key to make hot-reload possible after list updates.
    _ = mtime
    ranges: list[tuple[int, int]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = (raw_line or "").strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    net = ipaddress.ip_network(line, strict=False)
                except Exception:
                    continue
                if getattr(net, "version", None) != 4:
                    continue
                start = int(net.network_address)
                end = int(net.broadcast_address)
                ranges.append((start, end))
    except Exception:
        return ((), ())

    if not ranges:
        return ((), ())

    ranges.sort(key=lambda x: x[0])
    merged: list[list[int]] = []
    for start, end in ranges:
        if not merged or start > (merged[-1][1] + 1):
            merged.append([start, end])
            continue
        merged[-1][1] = max(merged[-1][1], end)

    starts = tuple(r[0] for r in merged)
    ends = tuple(r[1] for r in merged)
    return (starts, ends)


def _ipv4_in_index(ip_int: int, index: _IPv4RangeIndex) -> bool:
    starts, ends = index
    if not starts:
        return False
    i = bisect.bisect_right(starts, ip_int) - 1
    if i < 0:
        return False
    return ip_int <= ends[i]


def _is_blocked_by_country_cidrs(ip: str, blocked_codes: set[str]) -> bool:
    # Fast, GeoIP-free country blocking using CIDR lists (IPv4 only).
    if not blocked_codes:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(ip_obj, "version", None) != 4:
        # This service is currently IPv4-only at the edge (nginx listens on 0.0.0.0:*).
        return False
    ip_int = int(ip_obj)
    for code in blocked_codes:
        path = _zone_file_path(code)
        mtime = _file_mtime(path)
        if mtime <= 0:
            continue
        idx = _load_ipv4_range_index_from_zone_file(path, mtime)
        if _ipv4_in_index(ip_int, idx):
            return True
    return False


def is_blocked_country(req=None) -> bool:
    if is_internal_request(req):
        return False
    blocked = _blocked_country_codes()
    if not blocked:
        return False
    ip = get_client_ip(req)
    if not ip:
        return False
    code = _lookup_country_code(ip)
    if code:
        return code in blocked
    return _is_blocked_by_country_cidrs(ip, blocked)


def is_admin_or_internal(req=None) -> bool:
    try:
        if current_user.is_authenticated and (current_user.role or "").strip() == "admin":
            return True
    except Exception as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Swallowed exception in is_admin_or_internal: %s", exc, exc_info=True)
    return is_internal_request(req)


def check_admin_or_internal_access(req=None) -> tuple[bool, str]:
    if is_blocked_country(req):
        return False, "blocked_country"
    if is_admin_or_internal(req):
        return True, ""
    return False, "forbidden"
