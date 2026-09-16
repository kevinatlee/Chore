from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from chore.sqlite_backups import BackupError, create_daily_backup


class Command(BaseCommand):
    help = "Create the daily SQLite-safe Chore backup and retain the newest seven."

    def add_arguments(self, parser):
        parser.add_argument(
            "--if-exists",
            action="store_true",
            help="Exit successfully when the database does not exist (used before first migration).",
        )

    def handle(self, *args, **options):
        database_path = Path(settings.DATABASES["default"]["NAME"])
        if options["if_exists"] and not database_path.exists():
            self.stdout.write("Database does not exist yet; pre-migration backup skipped.")
            return
        try:
            path, created, removed = create_daily_backup(
                database_path,
                settings.CHORE_BACKUP_DIR,
                backup_date=timezone.localdate(),
                retention=settings.CHORE_BACKUP_RETENTION,
            )
        except (BackupError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        action = "Created" if created else "Retained existing"
        self.stdout.write(self.style.SUCCESS(f"{action} daily backup: {path.name}"))
        if removed:
            self.stdout.write(f"Pruned {len(removed)} expired backup(s).")
