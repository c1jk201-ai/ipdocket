import os
import sys
import time


def _config_name() -> str:
    # Prefer explicit config selector if provided.
    cfg = (os.environ.get("FLASK_CONFIG") or "").strip().lower()
    if cfg in {"development", "production", "default"}:
        return cfg

    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "").strip().lower()
    return "production" if env in {"prod", "production"} else "development"


def main() -> int:
    # This process is dedicated to background jobs only.
    # NOTE:
    # - .env.example intentionally sets SCHEDULER_ENABLED/RUN_SCHEDULER=0 for the web process.
    # - This scheduler worker must override those values deterministically.
    os.environ["SCHEDULER_ENABLED"] = "1"
    os.environ["SCHEDULER_PROCESS_ROLE"] = "worker"
    os.environ["RUN_SCHEDULER"] = "1"
    # Backward compat: some code paths still check this legacy flag.
    os.environ["SCHEDULER_RUN_ANYWAY"] = "1"
    # Never allow runtime schema auto-create in scheduler worker.
    os.environ["DB_SCHEMA_AUTO_CREATE"] = "0"

    from app import create_app

    app = create_app(_config_name(), enable_scheduler=True)
    scheduler = app.extensions.get("apscheduler")
    if scheduler is None:
        sys.stderr.write(
            "Scheduler did not start. Check DB connection and advisory-lock settings.\n"
        )
        return 1

    sys.stdout.write("Scheduler worker started.\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
