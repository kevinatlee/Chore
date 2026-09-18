from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from checklists.models import (
    ChecklistInstance,
    ChecklistItem,
    DiscrepancyExplanation,
    ScheduledReportDelivery,
    StaffContribution,
)


class Command(BaseCommand):
    help = (
        "Purge real operational checklist history older than seven calendar years, or all "
        "real runtime history for a clean operational start with --fresh-start. Generated "
        "mock history is preserved."
    )

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Use this YYYY-MM-DD retention date.")
        parser.add_argument(
            "--fresh-start",
            action="store_true",
            help=(
                "Remove all non-mock operational checklist history and scheduled report "
                "delivery history while preserving generated mock history, configuration, "
                "users, memberships, and the operational staff roster."
            ),
        )
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        fresh_start = options["fresh_start"]
        if fresh_start and options.get("as_of"):
            raise CommandError("--as-of cannot be used with --fresh-start.")

        mock_count = ChecklistInstance.objects.filter(is_mock_data=True).count()
        if fresh_start:
            instances = ChecklistInstance.objects.filter(is_mock_data=False)
            cutoff = None
        else:
            as_of = timezone.localdate()
            if options.get("as_of"):
                try:
                    as_of = date.fromisoformat(options["as_of"])
                except ValueError as exc:
                    raise CommandError("--as-of must be YYYY-MM-DD.") from exc
            try:
                cutoff = as_of.replace(year=as_of.year - 7)
            except ValueError:  # February 29 retains February 28 of the boundary year
                cutoff = as_of.replace(year=as_of.year - 7, day=28)
            instances = ChecklistInstance.objects.filter(
                operational_date__lt=cutoff,
                is_mock_data=False,
            )

        instance_count = instances.count()
        item_ids = ChecklistItem.objects.filter(instance__in=instances).values_list("pk", flat=True)
        item_count = item_ids.count()
        contribution_count = StaffContribution.objects.filter(item_id__in=item_ids).count()
        discrepancy_count = DiscrepancyExplanation.objects.filter(instance__in=instances).count()
        delivery_count = ScheduledReportDelivery.objects.count() if fresh_start else 0

        if fresh_start:
            summary = (
                f"{instance_count} non-mock checklist(s), {item_count} item(s), "
                f"{contribution_count} contribution(s), {discrepancy_count} discrepancy explanation(s), "
                f"{delivery_count} scheduled report "
                f"delivery record(s); preserving {mock_count} generated mock checklist(s)"
            )
        else:
            summary = (
                f"cutoff={cutoff.isoformat()}; {instance_count} non-mock checklist(s), "
                f"{item_count} item(s), {contribution_count} contribution(s), "
                f"{discrepancy_count} discrepancy explanation(s); preserving "
                f"{mock_count} generated mock checklist(s)"
            )

        if options["dry_run"]:
            mode = "fresh start" if fresh_start else "retention"
            self.stdout.write(f"Dry run ({mode}): {summary}")
            transaction.set_rollback(True)
            return

        StaffContribution.objects.filter(item_id__in=item_ids).delete()
        DiscrepancyExplanation.objects.filter(instance__in=instances).delete()
        ChecklistItem.objects.filter(pk__in=item_ids).delete()
        instances.delete()
        if fresh_start:
            ScheduledReportDelivery.objects.all().delete()
            self.stdout.write(self.style.SUCCESS(f"Fresh-start purge complete: {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Purged operational data: {summary}"))
