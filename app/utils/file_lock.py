import os
import time
from contextlib import contextmanager
from typing import IO, Optional

from app.utils.error_logging import report_swallowed_exception


def try_acquire_lock_file(lock_path: str) -> Optional[IO[str]]:
    """
    Best-effort cross-process lock (non-blocking).

    Returns an open, locked file handle on success; callers must keep it open to
    hold the lock and call `release_lock_file()` to release it.
    """
    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        try:
            os.makedirs(lock_dir, exist_ok=True)
        except Exception:
            return None
    try:
        f = open(lock_path, "a+", encoding="utf-8")
    except Exception:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        try:
            f.close()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="file_lock.try_acquire.close",
                log_key="file_lock.try_acquire.close",
                log_window_seconds=300,
            )
        return None


def release_lock_file(lock_file: IO[str]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        try:
            lock_file.close()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="file_lock.release.close",
                log_key="file_lock.release.close",
                log_window_seconds=300,
            )


@contextmanager
def file_lock(lock_path: str, *, timeout_seconds: int = 30, poll_interval: float = 0.1):
    """
    Cross-process lock using flock (Linux/Docker friendly).
    - Ensures only one ingestion worker touches the same mailbox checkpoint at a time.
    """
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    f = open(lock_path, "a+", encoding="utf-8")
    start = time.time()
    try:
        if os.name == "nt":
            # Best-effort on Windows; Docker prod is Linux.
            import msvcrt

            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() - start > timeout_seconds:
                        raise TimeoutError(f"Lock timeout: {lock_path}")
                    time.sleep(poll_interval)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() - start > timeout_seconds:
                        raise TimeoutError(f"Lock timeout: {lock_path}")
                    time.sleep(poll_interval)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                f.close()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="file_lock.close",
                    log_key="file_lock.close",
                    log_window_seconds=300,
                )
