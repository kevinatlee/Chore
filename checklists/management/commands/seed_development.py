import hashlib
import os
from datetime import time

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from checklists.models import (
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistSection,
    Program,
    ProgramMembership,
    ProgramRole,
    SectionTaskMembership,
    Shift,
    StaffCategory,
    StaffMember,
    TaskDefinition,
)
from checklists.seed_data import (
    ASSIGNMENTS,
    CATEGORIES,
    PROGRAMMING_TASK_LABEL,
    SHIFTS,
    STAFF_ROSTER,
)


def parse_time(value):
    return time.fromisoformat(value)


def stable_key(prefix, value):
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"phase5-{prefix}-{digest}"


class Command(BaseCommand):
    help = "Idempotently seed the authoritative Phase 5 Sonder House configuration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Apply password environment variables to existing development accounts.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        program, _ = Program.objects.update_or_create(
            slug="sonder-house", defaults={"name": "Sonder House", "is_active": True}
        )
        categories = {}
        for slug, name, sort_order in CATEGORIES:
            category, _ = StaffCategory.objects.update_or_create(
                program=program,
                slug=slug,
                defaults={"name": name, "sort_order": sort_order, "is_active": True},
            )
            categories[slug] = category

        shifts = {}
        for key, name, start, end, sort_order in SHIFTS:
            shift = Shift.objects.filter(seed_key=f"shift-{key}").first()
            if shift is None:
                shift = Shift.objects.filter(
                    start_time=parse_time(start), end_time=parse_time(end)
                ).first()
            if shift is None:
                shift = Shift(seed_key=f"shift-{key}")
            shift.seed_key = f"shift-{key}"
            shift.name = name
            shift.start_time = parse_time(start)
            shift.end_time = parse_time(end)
            shift.sort_order = sort_order
            shift.is_active = True
            shift.save()
            shifts[key] = shift

        # Superseded seed records remain available for historical foreign keys but
        # can no longer participate in future configuration.
        ChecklistSection.objects.exclude(seed_key__startswith="phase5-section-").update(is_active=False)
        TaskDefinition.objects.exclude(seed_key__startswith="phase5-task-").update(is_active=False)

        active_assignment_keys = []
        active_section_keys = set()
        active_task_keys = set()
        rendered_task_count = 0
        for category_key, shift_key, name, sort_order, sections in ASSIGNMENTS:
            assignment_key = f"definition-{category_key}-{shift_key}"
            active_assignment_keys.append(assignment_key)
            assignment, _ = ChecklistDefinition.objects.update_or_create(
                seed_key=assignment_key,
                defaults={
                    "name": name,
                    "category": categories[category_key],
                    "shift": shifts[shift_key],
                    "sort_order": sort_order,
                    "is_active": True,
                },
            )
            assignment.section_memberships.all().delete()
            for section_index, (section_name, task_specs) in enumerate(sections, start=1):
                rendered_task_count += len(task_specs)
                section_identity = section_name + "\n" + "\n".join(
                    f"{spec['label']}|{int(spec['allow_na'])}" for spec in task_specs
                )
                section_key = stable_key("section", section_identity)
                active_section_keys.add(section_key)
                section, _ = ChecklistSection.objects.update_or_create(
                    seed_key=section_key,
                    defaults={
                        "name": section_name,
                        "sort_order": section_index * 10,
                        "is_active": True,
                    },
                )
                AssignmentSectionMembership.objects.create(
                    assignment=assignment,
                    section=section,
                    sort_order=section_index * 10,
                )
                expected_task_ids = []
                for task_index, spec in enumerate(task_specs, start=1):
                    task_key = stable_key("task", spec["label"])
                    active_task_keys.add(task_key)
                    task_definition, _ = TaskDefinition.objects.update_or_create(
                        seed_key=task_key,
                        defaults={
                            "label": spec["label"],
                            "allow_na": spec["allow_na"],
                            "requires_completion_note": spec["label"] == PROGRAMMING_TASK_LABEL,
                            "is_active": True,
                            "scheduled_start": None,
                            "scheduled_end": None,
                        },
                    )
                    expected_task_ids.append(task_definition.pk)
                    SectionTaskMembership.objects.update_or_create(
                        section=section,
                        task=task_definition,
                        defaults={"sort_order": task_index * 10},
                    )
                section.task_memberships.exclude(task_id__in=expected_task_ids).delete()

        ChecklistDefinition.objects.filter(seed_key__startswith="definition-").exclude(
            seed_key__in=active_assignment_keys
        ).update(is_active=False)
        ChecklistSection.objects.filter(seed_key__startswith="phase5-section-").exclude(
            seed_key__in=active_section_keys
        ).update(is_active=False)
        TaskDefinition.objects.filter(seed_key__startswith="phase5-task-").exclude(
            seed_key__in=active_task_keys
        ).update(is_active=False)

        active_staff_keys = []
        for display_name in STAFF_ROSTER:
            first_name, last_name = display_name.split(" ", 1)
            seed_key = f"sonder-house-{slugify(display_name)}"
            active_staff_keys.append(seed_key)
            StaffMember.objects.update_or_create(
                seed_key=seed_key,
                defaults={
                    "program": program,
                    "first_name": first_name,
                    "last_name": last_name,
                    "is_active": True,
                },
            )
        StaffMember.objects.filter(seed_key__startswith="sonder-house-").exclude(
            seed_key__in=active_staff_keys
        ).update(is_active=False)

        operational_user = self._seed_user(
            username="sonderhouse",
            first_name="Sonder House",
            last_name="Operations",
            password_env="CHORE_OPERATIONAL_PASSWORD",
            reset_passwords=options["reset_passwords"],
            operational_program=program,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded Phase 5: 7 shift assignments, {len(active_section_keys)} reusable "
                f"sections, {len(active_task_keys)} reusable tasks, and {rendered_task_count} "
                "ordered assignment task placements."
            )
        )
        if not os.environ.get("CHORE_OPERATIONAL_PASSWORD") and not operational_user.has_usable_password():
            self.stdout.write(
                self.style.WARNING(
                    "The Sonder House operational account has no usable password. Set "
                    "CHORE_OPERATIONAL_PASSWORD and rerun with --reset-passwords."
                )
            )

    def _seed_user(
        self, *, username, first_name, last_name, password_env, reset_passwords,
        operational_program=None,
    ):
        User = get_user_model()
        user = User.objects.filter(username__iexact=username).first()
        created = user is None
        if created:
            user = User(username=username)
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.save()
        if operational_program is not None:
            ProgramMembership.objects.update_or_create(
                user=user,
                program=operational_program,
                defaults={
                    "role": ProgramRole.OPERATIONAL,
                    "is_active": True,
                    "receive_scheduled_reports": False,
                },
            )
        password = os.environ.get(password_env)
        if password and (created or reset_passwords):
            validate_password(password, user)
            user.set_password(password)
        elif created:
            user.set_unusable_password()
        user.save()
        return user
