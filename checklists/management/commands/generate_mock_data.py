import random
from collections import Counter
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from checklists.models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    Program,
    StaffContribution,
    StaffMember,
    TaskState,
)
from checklists.services import resolve_checklist


class Command(BaseCommand):
    help = "Generate deterministic reporting history using the real operational staff roster."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--seed", type=int, default=20260916)
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete only operational records marked as generated mock data.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()
            return
        days = options["days"]
        if days < 1:
            raise CommandError("--days must be at least 1.")
        program = Program.objects.filter(slug="sonder-house", is_active=True).first()
        if program is None:
            raise CommandError("Run seed_development before generating mock data.")
        staff = list(StaffMember.objects.filter(program=program, is_active=True))
        if not staff:
            raise CommandError("No active operational staff roster is available.")
        definitions = list(
            ChecklistDefinition.objects.filter(
                category__program=program,
                category__is_active=True,
                shift__is_active=True,
                is_active=True,
            ).select_related("category", "shift")
        )
        rng = random.Random(options["seed"])
        end = timezone.localdate()
        start = end - timedelta(days=days - 1)
        created_instances = 0
        created_items = 0
        changed_items = 0

        for day_offset in range(days):
            operational_date = start + timedelta(days=day_offset)
            for definition_index, definition in enumerate(definitions):
                if definition.category.slug == "life-skills" and operational_date.weekday() >= 5:
                    continue
                if ChecklistInstance.objects.filter(
                    program=program,
                    operational_date=operational_date,
                    category=definition.category,
                    shift=definition.shift,
                ).exists():
                    continue
                instance = resolve_checklist(definition, operational_date)
                instance.is_mock_data = True
                instance.save(update_fields=("is_mock_data",))
                created_instances += 1
                items = list(instance.items.all())
                created_items += len(items)
                mode_roll = rng.random()
                completion_target = 1.0 if mode_roll < 0.56 else rng.uniform(0.3, 0.88)
                contributors = [
                    staff[(created_instances + definition_index) % len(staff)]
                ]
                if len(items) > 1 and rng.random() < 0.58:
                    contributors.append(
                        staff[(created_instances + definition_index + 7) % len(staff)]
                    )
                contribution_rows = []
                changed_rows = []
                stamp = timezone.make_aware(
                    datetime.combine(operational_date, time(12)),
                    timezone.get_current_timezone(),
                )
                for item_index, item in enumerate(items):
                    if item.allow_na_snapshot and rng.random() < 0.07:
                        new_state = TaskState.NOT_APPLICABLE
                    elif rng.random() <= completion_target:
                        new_state = TaskState.COMPLETED
                    else:
                        continue
                    selected_staff = contributors[item_index % len(contributors)]
                    item.current_state = new_state
                    item.current_staff = selected_staff
                    item.state_changed_at = stamp + timedelta(minutes=item_index)
                    changed_rows.append(item)
                    contribution_rows.append(
                        StaffContribution(
                            item=item,
                            staff=selected_staff,
                            recorded_by=None,
                            previous_state=TaskState.PENDING,
                            new_state=new_state,
                            created_at=item.state_changed_at,
                        )
                    )
                if changed_rows:
                    ChecklistItem.objects.bulk_update(
                        changed_rows,
                        ("current_state", "current_staff", "state_changed_at", "updated_at"),
                    )
                    StaffContribution.objects.bulk_create(contribution_rows)
                    for contribution, item in zip(contribution_rows, changed_rows):
                        contribution.created_at = item.state_changed_at
                    StaffContribution.objects.bulk_update(
                        contribution_rows, ("created_at",)
                    )
                    changed_items += len(changed_rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"Generated mock data for {start.isoformat()} through {end.isoformat()}: "
                f"{created_instances} checklist(s), {created_items} task record(s), "
                f"{changed_items} state change(s)."
            )
        )
        self._print_summary(program)

    @transaction.atomic
    def _clear(self):
        instances = ChecklistInstance.objects.filter(is_mock_data=True)
        instance_count = instances.count()
        item_ids = ChecklistItem.objects.filter(instance__in=instances).values_list("pk", flat=True)
        contribution_count = StaffContribution.objects.filter(item_id__in=item_ids).count()
        StaffContribution.objects.filter(item_id__in=item_ids).delete()
        ChecklistItem.objects.filter(pk__in=item_ids).delete()
        instances.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared {instance_count} generated checklist(s) and "
                f"{contribution_count} generated contribution(s); configuration and roster retained."
            )
        )

    def _print_summary(self, program):
        instances = ChecklistInstance.objects.filter(program=program, is_mock_data=True)
        category_counts = Counter(instances.values_list("category_name_snapshot", flat=True))
        shift_counts = Counter(instances.values_list("shift_name_snapshot", flat=True))
        completed = 0
        incomplete = 0
        for instance in instances.prefetch_related("items"):
            if any(item.current_state == TaskState.PENDING for item in instance.items.all()):
                incomplete += 1
            else:
                completed += 1
        contributions = StaffContribution.objects.filter(item__instance__in=instances)
        self.stdout.write(f"Checklist instances: {instances.count()}")
        self.stdout.write(f"Task records: {ChecklistItem.objects.filter(instance__in=instances).count()}")
        self.stdout.write(f"Staff Contributions: {contributions.count()}")
        self.stdout.write(f"By category: {dict(sorted(category_counts.items()))}")
        self.stdout.write(f"By shift: {dict(sorted(shift_counts.items()))}")
        self.stdout.write(f"Completed checklists: {completed}; incomplete checklists: {incomplete}")
        self.stdout.write(
            f"N/A tasks: {ChecklistItem.objects.filter(instance__in=instances, current_state=TaskState.NOT_APPLICABLE).count()}"
        )
        self.stdout.write(
            f"Distinct operational staff represented: {contributions.values('staff_id').distinct().count()}"
        )
