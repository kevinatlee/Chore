import os
from datetime import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from checklists.models import (
    ChecklistDefinition,
    ChecklistSection,
    Shift,
    StaffAssignment,
    StaffCategory,
    TaskDefinition,
)
from checklists.seed_data import (
    ALLOW_NA_LABELS,
    CATEGORIES,
    DEFINITIONS,
    LIFE_SKILLS_SLOTS,
    SHIFTS,
)


def parse_time(value):
    return time.fromisoformat(value)


class Command(BaseCommand):
    help = "Idempotently seed Phase 1 development users and Sonder House configuration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Apply password environment variables to existing development accounts.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        categories = {}
        for slug, name, sort_order in CATEGORIES:
            category, _ = StaffCategory.objects.update_or_create(
                slug=slug,
                defaults={"name": name, "sort_order": sort_order, "is_active": True},
            )
            categories[slug] = category

        shifts = {}
        for key, name, start, end, sort_order in SHIFTS:
            shift, _ = Shift.objects.update_or_create(
                start_time=parse_time(start),
                end_time=parse_time(end),
                defaults={"name": name, "sort_order": sort_order, "is_active": True},
            )
            shifts[key] = shift

        for category_key, shift_key, name, sort_order, sections in DEFINITIONS:
            definition, _ = ChecklistDefinition.objects.update_or_create(
                category=categories[category_key],
                shift=shifts[shift_key],
                defaults={"name": name, "sort_order": sort_order, "is_active": True},
            )
            self._seed_standard_sections(definition, sections)

        life_definition, _ = ChecklistDefinition.objects.update_or_create(
            category=categories["life-skills"],
            shift=shifts["life-skills-day"],
            defaults={
                "name": "Life Skills Weekday Schedule",
                "sort_order": 10,
                "is_active": True,
            },
        )
        life_section, _ = ChecklistSection.objects.update_or_create(
            definition=life_definition,
            sort_order=10,
            defaults={"name": "Weekday Schedule", "is_active": True},
        )
        for order, (start, end, labels) in enumerate(LIFE_SKILLS_SLOTS, start=1):
            for weekday, label in enumerate(labels):
                TaskDefinition.objects.update_or_create(
                    section=life_section,
                    weekday=weekday,
                    sort_order=order * 10,
                    defaults={
                        "label": label,
                        "allow_na": False,
                        "is_active": True,
                        "scheduled_start": parse_time(start),
                        "scheduled_end": parse_time(end),
                    },
                )

        test_staff = self._seed_user(
            username="teststaff",
            first_name="Test",
            last_name="Staff",
            is_staff=False,
            is_superuser=False,
            password_env="CHORE_TEST_STAFF_PASSWORD",
            reset_passwords=options["reset_passwords"],
        )
        self._seed_user(
            username="choreadmin",
            first_name="Development",
            last_name="Admin",
            is_staff=True,
            is_superuser=True,
            password_env="CHORE_ADMIN_PASSWORD",
            reset_passwords=options["reset_passwords"],
        )
        for category in categories.values():
            StaffAssignment.objects.update_or_create(
                user=test_staff, category=category, defaults={"is_active": True}
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded Test Staff, development admin, 4 categories, 4 shifts, and 7 checklists."
            )
        )
        if not os.environ.get("CHORE_TEST_STAFF_PASSWORD"):
            self.stdout.write(
                self.style.WARNING(
                    "Test Staff has no usable password. Set CHORE_TEST_STAFF_PASSWORD and rerun "
                    "with --reset-passwords."
                )
            )
        if not os.environ.get("CHORE_ADMIN_PASSWORD"):
            self.stdout.write(
                self.style.WARNING(
                    "Development Admin has no usable password. Set CHORE_ADMIN_PASSWORD and rerun "
                    "with --reset-passwords."
                )
            )

    def _seed_standard_sections(self, definition, sections):
        for section_order, (section_name, labels) in enumerate(sections, start=1):
            section, _ = ChecklistSection.objects.update_or_create(
                definition=definition,
                sort_order=section_order * 10,
                defaults={"name": section_name, "is_active": True},
            )
            for task_order, label in enumerate(labels, start=1):
                TaskDefinition.objects.update_or_create(
                    section=section,
                    weekday=None,
                    sort_order=task_order * 10,
                    defaults={
                        "label": label,
                        "allow_na": label in ALLOW_NA_LABELS,
                        "is_active": True,
                        "scheduled_start": None,
                        "scheduled_end": None,
                    },
                )

    def _seed_user(
        self,
        *,
        username,
        first_name,
        last_name,
        is_staff,
        is_superuser,
        password_env,
        reset_passwords,
    ):
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = True
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        password = os.environ.get(password_env)
        if password and (created or reset_passwords):
            user.set_password(password)
        elif created:
            user.set_unusable_password()
        user.save()
        return user

