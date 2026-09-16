import time as time_module
from datetime import datetime, time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


SCHEDULE_TIMES = (time(2), time(8))


class Command(BaseCommand):
    help = "Run daily backups and scheduled Manager reports in the business timezone."

    def handle(self, *args, **options):
        self.stdout.write(
            "Production scheduler started (backups 02:00; reports 08:00 America/Vancouver)."
        )
        while True:
            call_command("backup_database")
            try:
                call_command("send_scheduled_reports")
            except CommandError as exc:
                # An SMTP outage must remain visible without taking down the web app.
                # Failed delivery records are retried by the next due scheduler run.
                self.stderr.write(self.style.ERROR(f"Scheduled report failure: {exc}"))
            now = timezone.localtime()
            next_run = self._next_run(now)
            self.stdout.write(f"Next production scheduler run: {next_run.isoformat()}")
            try:
                time_module.sleep(max((next_run - now).total_seconds(), 60))
            except KeyboardInterrupt:
                self.stdout.write("Production scheduler stopped.")
                return

    def _next_run(self, now):
        for scheduled_time in SCHEDULE_TIMES:
            candidate = timezone.make_aware(
                datetime.combine(now.date(), scheduled_time),
                timezone.get_current_timezone(),
            )
            if candidate > now:
                return candidate
        return timezone.make_aware(
            datetime.combine(now.date() + timedelta(days=1), SCHEDULE_TIMES[0]),
            timezone.get_current_timezone(),
        )
