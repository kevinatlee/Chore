import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path


BACKUP_FILENAME_RE = re.compile(r"^chore-(\d{4}-\d{2}-\d{2})\.sqlite3$")


class BackupError(RuntimeError):
    pass


def _read_only_connection(path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def validate_chore_database(path):
    """Return True only for a readable, internally consistent Chore database."""
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with closing(_read_only_connection(path)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                return False
            migration_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'django_migrations'"
            ).fetchone()
            return migration_table is not None
    except (OSError, sqlite3.DatabaseError):
        return False


def _dated_backups(backup_dir):
    backup_dir = Path(backup_dir)
    backups = []
    if not backup_dir.is_dir():
        return backups
    for candidate in backup_dir.iterdir():
        match = BACKUP_FILENAME_RE.fullmatch(candidate.name)
        if candidate.is_file() and match:
            try:
                backup_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            backups.append((backup_date, candidate))
    return sorted(backups, key=lambda item: (item[0], item[1].name), reverse=True)


def prune_backups(backup_dir, retention=7):
    if retention < 1:
        raise BackupError("Backup retention must be at least one day.")
    removed = []
    for _, path in _dated_backups(backup_dir)[retention:]:
        path.unlink()
        removed.append(path)
    return removed


def create_daily_backup(source_database, backup_dir, *, backup_date, retention=7):
    """Create one atomic SQLite-online-backup snapshot for the supplied local date."""
    source_database = Path(source_database)
    backup_dir = Path(backup_dir)
    if not validate_chore_database(source_database):
        raise BackupError(f"Source is not a valid Chore SQLite database: {source_database}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"chore-{backup_date.isoformat()}.sqlite3"
    if validate_chore_database(destination):
        return destination, False, prune_backups(backup_dir, retention)

    temporary = backup_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with closing(_read_only_connection(source_database)) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
        if not validate_chore_database(temporary):
            raise BackupError("SQLite backup validation failed.")
        with temporary.open("r+b") as backup_file:
            backup_file.flush()
            os.fsync(backup_file.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    return destination, True, prune_backups(backup_dir, retention)


def newest_valid_backup(backup_dir):
    try:
        for _, candidate in _dated_backups(backup_dir):
            if validate_chore_database(candidate):
                return candidate
    except OSError as exc:
        raise BackupError(f"Cannot read production backup directory: {backup_dir}") from exc
    raise BackupError(f"No valid production backup exists in {Path(backup_dir)}.")


def _path_is_within(path, directory):
    return path == directory or directory in path.parents


def _copy_sqlite_snapshot(source_database, destination):
    with closing(_read_only_connection(source_database)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)


def _refresh_recovery_backup(destination_database):
    if not destination_database.exists():
        return None
    if not validate_chore_database(destination_database):
        raise BackupError(
            f"Existing ChoreTest database is invalid; refusing replacement: {destination_database}"
        )
    recovery_dir = destination_database.parent / "refresh-backups"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recovery = recovery_dir / f"choretest-before-refresh-{timestamp}.sqlite3"
    if recovery.exists():
        recovery = recovery_dir / (
            f"choretest-before-refresh-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
    _copy_sqlite_snapshot(destination_database, recovery)
    if not validate_chore_database(recovery):
        recovery.unlink(missing_ok=True)
        raise BackupError("Pre-refresh ChoreTest recovery backup failed validation.")
    return recovery


def refresh_database_from_backup(
    backup_dir, destination_database, *, source_database=None
):
    """Safely and atomically replace a stopped ChoreTest database."""
    backup_dir = Path(backup_dir).resolve()
    destination_database = Path(destination_database).resolve()
    if _path_is_within(destination_database, backup_dir):
        raise BackupError("The test database must not be stored in the backup directory.")

    if source_database is None:
        source = newest_valid_backup(backup_dir)
    else:
        source = Path(source_database).resolve()
        if not _path_is_within(source, backup_dir):
            raise BackupError("The selected snapshot must be inside the read-only backup directory.")
        if not validate_chore_database(source):
            raise BackupError(f"Selected snapshot is not a valid Chore SQLite database: {source}")
    if source == destination_database:
        raise BackupError("Source and ChoreTest destination must be different files.")

    destination_database.parent.mkdir(parents=True, exist_ok=True)
    existing_stat = destination_database.stat() if destination_database.exists() else None
    recovery = _refresh_recovery_backup(destination_database)
    temporary = destination_database.parent / (
        f".{destination_database.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        if existing_stat is not None:
            os.chmod(temporary, existing_stat.st_mode)
            if hasattr(os, "chown"):
                try:
                    os.chown(temporary, existing_stat.st_uid, existing_stat.st_gid)
                except PermissionError:
                    pass
        if not validate_chore_database(temporary):
            raise BackupError("Copied ChoreTest database failed validation.")
        with temporary.open("r+b") as copied_file:
            copied_file.flush()
            os.fsync(copied_file.fileno())
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{destination_database}{suffix}").unlink(missing_ok=True)
        temporary.replace(destination_database)
        if not validate_chore_database(destination_database):
            if recovery is not None:
                shutil.copy2(recovery, temporary)
                temporary.replace(destination_database)
            raise BackupError("Refreshed ChoreTest database failed post-replacement validation.")
    finally:
        temporary.unlink(missing_ok=True)
    return source, destination_database
