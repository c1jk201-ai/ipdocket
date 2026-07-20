"""Upload intake guardrails shared by document upload flows."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from flask import current_app, has_app_context

from app.services.uploads.upload_validation import sniff_extension_mismatch
from app.utils.error_logging import report_swallowed_exception

T = TypeVar("T")


class UploadSecurityError(ValueError):
    """Raised when an upload fails intake validation or malware scanning."""


class ParserTimeoutError(TimeoutError):
    """Raised when a document parser exceeds its configured timeout."""


@dataclass
class IntakeEvidence:
    kind: str
    detail: str
    status: str = "ok"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntakeValidationResult:
    filename: str
    extension: str
    ok: bool
    evidence: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cfg_int(key: str, default: int) -> int:
    try:
        if has_app_context():
            value = current_app.config.get(key)
            if value not in (None, ""):
                return int(value)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="uploads.intake_security.cfg_int.app_config",
            log_key="uploads.intake_security.cfg_int.app_config",
            log_window_seconds=300,
        )
    try:
        env_value = os.environ.get(key)
        if env_value:
            return int(env_value)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="uploads.intake_security.cfg_int.environ",
            log_key="uploads.intake_security.cfg_int.environ",
            log_window_seconds=300,
        )
    return int(default)


def parser_timeout_seconds(default: int = 20) -> int:
    return max(0, _cfg_int("UPLOAD_PARSER_TIMEOUT_SECONDS", default))


def virus_scan_timeout_seconds(default: int = 30) -> int:
    return max(1, _cfg_int("UPLOAD_VIRUS_SCAN_TIMEOUT_SECONDS", default))


def _virus_scan_command() -> str:
    if os.environ.get("TESTING") == "1":
        return ""
    try:
        if has_app_context():
            return str(current_app.config.get("UPLOAD_VIRUS_SCAN_COMMAND") or "").strip()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="uploads.intake_security.virus_scan_command",
            log_key="uploads.intake_security.virus_scan_command",
            log_window_seconds=300,
        )
    return str(os.environ.get("UPLOAD_VIRUS_SCAN_COMMAND") or "").strip()


def virus_scan_enabled() -> bool:
    """Return whether a malware scan command is configured for this process."""
    return bool(_virus_scan_command())


def virus_scan_mode() -> str:
    """
    Return the configured scan mode.

    - async: FileAsset uploads are accepted as pending and scanned by a worker.
    - sync: FileAsset uploads are scanned in the request path.
    - disabled: Skip scanning even when a command is present.
    """
    raw = ""
    try:
        if has_app_context():
            raw = str(current_app.config.get("UPLOAD_VIRUS_SCAN_MODE") or "").strip()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="uploads.intake_security.virus_scan_mode",
            log_key="uploads.intake_security.virus_scan_mode",
            log_window_seconds=300,
        )
    if not raw:
        raw = str(os.environ.get("UPLOAD_VIRUS_SCAN_MODE") or "").strip()
    mode = raw.lower()
    if mode in {"off", "false", "0", "disabled", "disable", "none"}:
        return "disabled"
    if mode in {"sync", "synchronous", "inline"}:
        return "sync"
    return "async"


def _virus_scan_fail_open() -> bool:
    try:
        if has_app_context():
            return bool(current_app.config.get("UPLOAD_VIRUS_SCAN_FAIL_OPEN", False))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="uploads.intake_security.virus_scan_fail_open",
            log_key="uploads.intake_security.virus_scan_fail_open",
            log_window_seconds=300,
        )
    return str(os.environ.get("UPLOAD_VIRUS_SCAN_FAIL_OPEN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_upload_path(
    path: str | Path,
    *,
    filename: str,
    allowed_exts: set[str],
) -> IntakeValidationResult:
    """Validate extension/content sniffing for a staged upload path."""
    name = (filename or "").strip()
    ext = Path(name).suffix.lower()
    result = IntakeValidationResult(filename=name, extension=ext, ok=True)
    if ext not in allowed_exts:
        result.ok = False
        result.errors.append("extension_not_allowed")
        return result

    try:
        mismatch = sniff_extension_mismatch(Path(path), filename=name)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="uploads.intake_security.validate_upload_path.sniff",
            log_key="uploads.intake_security.validate_upload_path.sniff",
            log_window_seconds=300,
        )
        mismatch = None

    if mismatch:
        result.ok = False
        result.errors.append("content_sniff_mismatch")
        result.evidence.append(
            asdict(
                IntakeEvidence(
                    kind="content_sniff",
                    status="rejected",
                    detail=mismatch,
                    data={"extension": ext},
                )
            )
        )
    else:
        result.evidence.append(
            asdict(
                IntakeEvidence(
                    kind="content_sniff",
                    status="ok",
                    detail="signature matched extension or no strict signature required",
                    data={"extension": ext},
                )
            )
        )
    return result


def scan_upload_path(path: str | Path, *, filename: str | None = None) -> dict[str, Any]:
    """
    Run an optional virus scan command against a local file.

    Configure with UPLOAD_VIRUS_SCAN_COMMAND. The command may include "{path}";
    if omitted, the file path is appended as the last argument. Non-zero exit
    means rejected unless UPLOAD_VIRUS_SCAN_FAIL_OPEN is enabled.
    """
    command = _virus_scan_command()
    if not command:
        return {"status": "disabled"}

    raw_parts = shlex.split(command)
    if not raw_parts:
        return {"status": "disabled"}

    path_str = str(Path(path))
    has_placeholder = any("{path}" in part for part in raw_parts)
    args = [part.replace("{path}", path_str) for part in raw_parts]
    if not has_placeholder:
        args.append(path_str)

    try:
        completed = subprocess.run(  # noqa: S603
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=virus_scan_timeout_seconds(),
        )
    except subprocess.TimeoutExpired as exc:
        payload = {"status": "timeout", "filename": filename, "timeout_seconds": exc.timeout}
        if _virus_scan_fail_open():
            payload["fail_open"] = True
            return payload
        raise UploadSecurityError("virus_scan_timeout") from exc
    except Exception as exc:
        payload = {"status": "error", "filename": filename, "error": type(exc).__name__}
        if _virus_scan_fail_open():
            payload["fail_open"] = True
            return payload
        raise UploadSecurityError("virus_scan_failed") from exc

    output = "\n".join(
        part.strip()
        for part in (completed.stdout[-800:], completed.stderr[-800:])
        if part and part.strip()
    )
    if completed.returncode == 0:
        status = "ok"
    elif completed.returncode == 1:
        status = "rejected"
    else:
        status = "error"

    payload = {
        "status": status,
        "returncode": int(completed.returncode),
        "filename": filename,
        "output_tail": output,
    }
    if completed.returncode != 0:
        if status == "rejected":
            raise UploadSecurityError("virus_scan_rejected")
        if _virus_scan_fail_open():
            payload["fail_open"] = True
            return payload
        raise UploadSecurityError("virus_scan_failed")
    return payload


def scan_upload_bytes(data: bytes, *, filename: str | None = None) -> dict[str, Any]:
    """Run the optional virus scanner for in-memory uploads."""
    if not _virus_scan_command():
        return {"status": "disabled"}
    suffix = Path(filename or "").suffix or ".bin"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="ipm_scan_", suffix=suffix, delete=False
        ) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        return scan_upload_path(tmp_path, filename=filename)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                report_swallowed_exception(
                    exc,
                    context="uploads.intake_security.scan_upload_bytes.cleanup",
                    log_key="uploads.intake_security.scan_upload_bytes.cleanup",
                    log_window_seconds=300,
                )


@contextmanager
def _alarm_timeout(seconds: int, label: str) -> Iterator[None]:
    sigalrm = getattr(signal, "SIGALRM", None)
    itimer_real = getattr(signal, "ITIMER_REAL", None)
    setitimer = getattr(signal, "setitimer", None)
    if sigalrm is None or itimer_real is None or not callable(setitimer):
        yield
        return

    def _handler(_signum, _frame):
        raise ParserTimeoutError(f"{label} timed out after {seconds}s")

    old_handler = signal.getsignal(sigalrm)
    try:
        signal.signal(sigalrm, _handler)
        setitimer(itimer_real, float(seconds))
        yield
    finally:
        setitimer(itimer_real, 0)
        signal.signal(sigalrm, old_handler)


def run_parser_with_timeout(
    func: Callable[[], T],
    *,
    timeout_seconds: int | None = None,
    label: str = "parser",
) -> T:
    """Run a parser under a best-effort wall-clock timeout."""
    seconds = parser_timeout_seconds() if timeout_seconds is None else int(timeout_seconds or 0)
    if seconds <= 0:
        return func()

    # SIGALRM is the only practical way to interrupt CPU-bound parser code in-process.
    # It is only safe from the main thread on Unix; other contexts run without the alarm.
    if (
        getattr(signal, "SIGALRM", None) is not None
        and threading.current_thread() is threading.main_thread()
    ):
        with _alarm_timeout(seconds, label):
            return func()
    return func()
