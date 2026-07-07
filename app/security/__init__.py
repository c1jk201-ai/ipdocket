from __future__ import annotations

from flask import Flask

from .cidr import init_cidr_guards
from .country_block import init_country_block
from .headers import init_security_headers
from .policy_engine import init_policy_engine


def init_security(app: Flask) -> None:
    """
    Central security initializer:
    - Security headers (CSP/HSTS/etc.)
    - CIDR allowlist guards for admin/internal routes
    - Country-based blocks (CN/RU, etc.)
    - Authorization policy enforcement for read queries
    """
    init_security_headers(app)
    init_cidr_guards(app)
    init_country_block(app)
    init_policy_engine()
