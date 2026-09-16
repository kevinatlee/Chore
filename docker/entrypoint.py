import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from chore.sqlite_backups import BackupError, refresh_database_from_backup


TRUE_VALUES = {"1", "true", "yes", "on"}


def enabled(name, default=False):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in TRUE_VALUES


def run_manage(*arguments):
    subprocess.run(
        [sys.executable, "manage.py", *arguments],
        check=True,
        env=os.environ.copy(),
    )


def refresh_test_database():
    backup_dir = os.environ.get("CHORE_PRODUCTION_BACKUP_DIR", "/production-backups")
    destination = os.environ.get("CHORE_DATABASE_PATH", "/app/data/db.sqlite3")
    try:
        source, copied_database = refresh_database_from_backup(backup_dir, destination)
    except (BackupError, OSError) as exc:
        raise SystemExit(f"ChoreTest startup failed: {exc}") from exc
    print(
        f"ChoreTest database refreshed from {source.name} into {copied_database}.",
        flush=True,
    )


def start_process(command):
    print(f"Starting: {' '.join(command)}", flush=True)
    return subprocess.Popen(command, env=os.environ.copy())


def supervise(processes, application):
    stopping = False

    def forward(signum, _frame):
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    exit_code = 0
    try:
        while True:
            application_status = application.poll()
            if application_status is not None:
                exit_code = application_status
                break
            for process in processes:
                status = process.poll()
                if process is not application and status is not None and not stopping:
                    print(
                        f"Required background process exited unexpectedly with status {status}.",
                        file=sys.stderr,
                        flush=True,
                    )
                    exit_code = status or 1
                    stopping = True
                    application.terminate()
                    break
            if stopping and application.poll() is not None:
                exit_code = application.returncode
                break
            time.sleep(0.5)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 10
        for process in processes:
            remaining = max(deadline - time.monotonic(), 0)
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
    return exit_code


def main():
    mode = os.environ.get("CHORE_DEPLOYMENT_MODE", "").strip().lower()
    if mode not in {"production", "test"}:
        raise SystemExit("CHORE_DEPLOYMENT_MODE must be 'production' or 'test'.")
    if enabled("DJANGO_DEBUG", default=True):
        raise SystemExit("Container deployments require DJANGO_DEBUG=false.")
    if mode == "production" and not enabled("CHORE_BACKUPS_ENABLED"):
        raise SystemExit("Production requires CHORE_BACKUPS_ENABLED=true.")

    Path(os.environ.get("CHORE_DATABASE_PATH", "/app/data/db.sqlite3")).parent.mkdir(
        parents=True, exist_ok=True
    )
    if mode == "test":
        if enabled("EMAIL_ENABLED", default=True):
            raise SystemExit("ChoreTest requires EMAIL_ENABLED=false.")
        refresh_test_database()
    else:
        run_manage("backup_database", "--if-exists")

    run_manage("migrate", "--noinput")
    run_manage("collectstatic", "--noinput", "--clear")

    processes = []
    if mode == "production":
        processes.append(
            start_process([sys.executable, "manage.py", "run_production_scheduler"])
        )

    gunicorn = start_process(
        [
            "gunicorn",
            "chore.wsgi:application",
            "--bind",
            "0.0.0.0:8000",
            "--workers",
            os.environ.get("GUNICORN_WORKERS", "2"),
            "--timeout",
            os.environ.get("GUNICORN_TIMEOUT", "60"),
            "--access-logfile",
            "-",
            "--error-logfile",
            "-",
        ]
    )
    processes.append(gunicorn)
    raise SystemExit(supervise(processes, gunicorn))


if __name__ == "__main__":
    main()
