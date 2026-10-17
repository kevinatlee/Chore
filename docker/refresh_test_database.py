import argparse
import os
from pathlib import Path

from chore.sqlite_backups import BackupError, refresh_database_from_backup, validate_chore_database
from docker.entrypoint import enabled


def parser():
    command = argparse.ArgumentParser(
        description="Explicitly refresh a stopped ChoreTest database from a validated snapshot."
    )
    command.add_argument("--database", default=os.environ.get("CHORE_DATABASE_PATH", "/app/data/db.sqlite3"))
    command.add_argument("--backup-dir", default=os.environ.get("CHORE_PRODUCTION_BACKUP_DIR", "/production-backups"))
    command.add_argument("--source", help="Specific snapshot inside --backup-dir; newest valid daily backup is used by default.")
    command.add_argument("--confirm", required=True, choices=("ChoreTest",))
    command.add_argument("--application-stopped", action="store_true", required=True)
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    if os.environ.get("CHORE_DEPLOYMENT_MODE", "").strip().lower() != "test":
        raise SystemExit("Refusing refresh: CHORE_DEPLOYMENT_MODE must be test.")
    if enabled("EMAIL_ENABLED", default=True):
        raise SystemExit("Refusing refresh: EMAIL_ENABLED must be false for ChoreTest.")

    destination = Path(args.database).resolve()
    protected = {
        Path("/mnt/user/appdata/Chore/db.sqlite3").resolve(),
    }
    configured_production = os.environ.get("CHORE_PRODUCTION_DATABASE_PATH")
    if configured_production:
        protected.add(Path(configured_production).resolve())
    if destination in protected:
        raise SystemExit(f"Refusing refresh against protected production path: {destination}")

    try:
        source, copied = refresh_database_from_backup(
            args.backup_dir,
            destination,
            source_database=args.source,
        )
    except (BackupError, OSError) as exc:
        raise SystemExit(f"ChoreTest refresh failed: {exc}") from exc
    if not validate_chore_database(copied):
        raise SystemExit("ChoreTest refresh failed final integrity verification.")
    recovery_dir = copied.parent / "refresh-backups"
    print(f"ChoreTest database refreshed from read-only snapshot: {source}")
    print(f"Validated destination: {copied}")
    print(f"Pre-refresh recovery backups: {recovery_dir}")
    print("Email remains disabled by EMAIL_ENABLED=false.")


if __name__ == "__main__":
    main()
