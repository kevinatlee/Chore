from datetime import datetime, time

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from checklists.models import (
    DeliveryState,
    Program,
    ProgramMembership,
    ProgramRole,
    ScheduledReportDelivery,
)
from checklists.reporting import build_report, report_snapshot, scheduled_periods


class Command(BaseCommand):
    help = "Send due Program-scoped Manager reports; safe to invoke repeatedly."

    def add_arguments(self, parser):
        parser.add_argument(
            "--at",
            help="Evaluate schedules at this ISO datetime (primarily for deterministic operations/tests).",
        )

    def handle(self, *args, **options):
        local_now = self._local_now(options.get("at"))
        if local_now.time() < time(8):
            self.stdout.write("No scheduled reports are due before 08:00 local time.")
            return

        failures = []
        sent = 0
        for program in Program.objects.filter(is_active=True):
            recipients = ProgramMembership.objects.filter(
                program=program,
                role=ProgramRole.MANAGER,
                is_active=True,
                receive_scheduled_reports=True,
                user__is_active=True,
            ).exclude(user__email="").select_related("user")
            if not recipients:
                continue
            for cadence, period in scheduled_periods(local_now.date()):
                report = build_report(program=program, period=period, include_test=False)
                snapshot = report_snapshot(report)
                subject = (
                    f"Chore {cadence} report — {program.name} — "
                    f"{period.start.isoformat()} to {period.end.isoformat()}"
                )
                body_html = render_to_string(
                    "checklists/email_report.html",
                    {"report": report, "subject": subject},
                )
                for membership in recipients:
                    delivery, created = ScheduledReportDelivery.objects.get_or_create(
                        program=program,
                        cadence=cadence,
                        period_start=period.start,
                        period_end=period.end,
                        recipient=membership.user,
                        defaults={
                            "recipient_email": membership.user.email,
                            "subject": subject,
                            "body_html": body_html,
                            "snapshot": snapshot,
                        },
                    )
                    if not created and delivery.state == DeliveryState.SENT:
                        continue
                    delivery.recipient_email = membership.user.email
                    delivery.subject = subject
                    delivery.body_html = body_html
                    delivery.snapshot = snapshot
                    delivery.state = DeliveryState.PENDING
                    delivery.error_message = ""
                    delivery.save()
                    try:
                        message = EmailMultiAlternatives(
                            subject=subject,
                            body=strip_tags(body_html),
                            from_email=settings.DEFAULT_FROM_EMAIL,
                            to=[membership.user.email],
                        )
                        message.attach_alternative(body_html, "text/html")
                        message.send(fail_silently=False)
                    except Exception as exc:  # backend-specific failures are recorded for retry
                        delivery.state = DeliveryState.FAILED
                        delivery.error_message = str(exc)[:2000]
                        delivery.save(update_fields=("state", "error_message"))
                        failures.append(f"{program.slug}/{cadence}/{membership.user.email}: {exc}")
                    else:
                        delivery.state = DeliveryState.SENT
                        delivery.sent_at = timezone.now()
                        delivery.save(update_fields=("state", "sent_at"))
                        sent += 1
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} scheduled report email(s)."))
        if failures:
            raise CommandError("; ".join(failures))

    def _local_now(self, raw):
        if not raw:
            return timezone.localtime()
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise CommandError("--at must be an ISO datetime.") from exc
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_current_timezone())
        return timezone.localtime(value)
