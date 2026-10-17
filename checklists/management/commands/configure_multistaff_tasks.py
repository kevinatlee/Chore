from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from checklists.models import TaskDefinition


TARGETS = (
    ("phase5-task-0ea65e10607f1b3945a0", "Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list"),
    ("phase5-task-03a8dcc33316f2ca4e41", "Review Heat Hut"),
    ("phase5-task-ca72368919b93134d763", "Check and reply to emails"),
    ("phase5-task-08179a09b70ca6748b49", "Complete leave requests and maintenance requests"),
    ("phase5-task-e07e816fcb3b44ca0aad", "Submit leave requests"),
    ("phase5-task-856c28710c15e5859166", "Complete Citation training"),
)


class Command(BaseCommand):
    help = "Audit or idempotently activate the reviewed multi-staff task definitions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Set Required Staff Completions to 2 after every exact target matches.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        resolved = []
        problems = []
        for seed_key, label in TARGETS:
            matches = list(TaskDefinition.objects.filter(seed_key=seed_key, label=label))
            if len(matches) != 1:
                problems.append(f"{seed_key}: expected one exact match, found {len(matches)}")
                continue
            resolved.append(matches[0])
        if problems:
            raise CommandError(
                "Refusing ambiguous multi-staff task activation:\n" + "\n".join(problems)
            )

        for task in resolved:
            relationships = ", ".join(
                f"{membership.section.name} (order {membership.sort_order})"
                for membership in task.section_memberships.select_related("section").order_by(
                    "section__name", "sort_order", "id"
                )
            )
            self.stdout.write(
                f"id={task.pk} seed_key={task.seed_key} label={task.label!r} "
                f"allow_na={task.allow_na} current={task.required_staff_completions} "
                f"sections=[{relationships}]"
            )
        if options["apply"]:
            TaskDefinition.objects.filter(pk__in=[task.pk for task in resolved]).update(
                required_staff_completions=2
            )
            self.stdout.write(self.style.SUCCESS(f"Updated {len(resolved)} exact task definitions."))
        else:
            self.stdout.write("Audit only; rerun with --apply to update exact matches.")

