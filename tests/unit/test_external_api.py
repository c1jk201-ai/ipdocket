from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from app.utils import external_api


@pytest.fixture(autouse=True)
def clear_external_api_state():
    with external_api._LOCK:
        external_api._STATE.clear()
    yield
    with external_api._LOCK:
        external_api._STATE.clear()


def _http_error(status_code: int, *, retry_after: str | None = None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    exc = requests.HTTPError(f"HTTP {status_code}")
    exc.response = response
    return exc


def test_external_api_call_respects_retry_after_delta_seconds(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    monkeypatch.setattr(external_api.time, "sleep", lambda seconds: sleeps.append(seconds))

    def flaky_call():
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429, retry_after="3")
        return "ok"

    assert external_api.external_api_call("test", "retry_after_delta", flaky_call) == "ok"
    assert calls["count"] == 2
    assert sleeps == [3.0]


def test_external_api_call_caps_retry_after_to_max_delay(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    monkeypatch.setattr(external_api.time, "sleep", lambda seconds: sleeps.append(seconds))

    def flaky_call():
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429, retry_after="120")
        return "ok"

    assert external_api.external_api_call("test", "retry_after_cap", flaky_call) == "ok"
    assert sleeps == [8.0]


def test_retry_after_http_date_parser_returns_positive_seconds() -> None:
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    raw = format_datetime(retry_at, usegmt=True)

    parsed = external_api._retry_after_seconds_from_value(raw)

    assert parsed is not None
    assert 0 < parsed <= 5


def test_retry_after_parser_reads_lowercase_mapping_header() -> None:
    class GoogleStyleError(Exception):
        resp = {"retry-after": "2"}

    assert external_api._retry_after_seconds_from_exc(GoogleStyleError()) == 2.0


def test_external_api_call_keeps_jittered_backoff_without_retry_after(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    monkeypatch.setattr(external_api.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(external_api.time, "sleep", lambda seconds: sleeps.append(seconds))

    def flaky_call():
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(503)
        return "ok"

    assert external_api.external_api_call("test", "retry_without_header", flaky_call) == "ok"
    assert sleeps == [0.5]


def test_external_api_call_retries_stdlib_timeout(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    monkeypatch.setattr(external_api.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(external_api.time, "sleep", lambda seconds: sleeps.append(seconds))

    def flaky_call():
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("_ssl.c:999: The handshake operation timed out")
        return "ok"

    assert external_api.external_api_call("test", "stdlib_timeout", flaky_call) == "ok"
    assert calls["count"] == 2
    assert sleeps == [0.5]
