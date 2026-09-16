from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from checklists.models import ChecklistInstance, ChecklistItem, StaffContribution


class Command(BaseCommand):
    help = "Purge operational checklists older than seven calendar years."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Use this YYYY-MM-DD retention date.")
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
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

        instances = ChecklistInstance.objects.filter(operational_date__lt=cutoff)
        instance_count = instances.count()
        item_ids = ChecklistItem.objects.filter(instance__in=instances).values_list("pk", flat=True)
        item_count = item_ids.count()
        contribution_count = StaffContribution.objects.filter(item_id__in=item_ids).count()
        summary = (
            f"cutoff={cutoff.isoformat()}; {instance_count} checklist(s), "
            f"{item_count} item(s), {contribution_count} contribution(s)"
        )
        if options["dry_run"]:
            self.stdout.write(f"Dry run: {summary}")
            transaction.set_rollback(True)
            return

        StaffContribution.objects.filter(item_id__in=item_ids).delete()
        ChecklistItem.objects.filter(pk__in=item_ids).delete()
        instances.delete()
        self.stdout.write(self.style.SUCCESS(f"Purged operational data: {summary}"))
