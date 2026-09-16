import os
from datetime import time

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from checklists.models import (
    ChecklistDefinition,
    ChecklistSection,
    Program,
    ProgramMembership,
    ProgramRole,
    Shift,
    StaffCategory,
    StaffMember,
    TaskDefinition,
)
from checklists.seed_data import (
    ALLOW_NA_LABELS,
    CATEGORIES,
    DEFINITIONS,
    LIFE_SKILLS_SLOTS,
    RETIRED_LIFE_SKILLS_SLOTS,
    SHIFTS,
    STAFF_ROSTER,
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
        program, _ = Program.objects.update_or_create(
            slug="sonder-house",
            defaults={"name": "Sonder House", "is_active": True},
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
            shift = self._upsert_seeded(
                Shift,
                seed_key=f"shift-{key}",
                legacy_lookup={
                    "start_time": parse_time(start),
                    "end_time": parse_time(end),
                },
                defaults={
                    "name": name,
                    "start_time": parse_time(start),
                    "end_time": parse_time(end),
                    "sort_order": sort_order,
                    "is_active": True,
                },
            )
            shifts[key] = shift

        for category_key, shift_key, name, sort_order, sections in DEFINITIONS:
            definition = self._upsert_seeded(
                ChecklistDefinition,
                seed_key=f"definition-{category_key}-{shift_key}",
                legacy_lookup={
                    "category": categories[category_key],
                    "shift": shifts[shift_key],
                },
                defaults={
                    "name": name,
                    "category": categories[category_key],
                    "shift": shifts[shift_key],
                    "sort_order": sort_order,
                    "is_active": True,
                },
            )
            self._seed_standard_sections(
                definition, category_key, shift_key, sections
            )

        life_definition = self._upsert_seeded(
            ChecklistDefinition,
            seed_key="definition-life-skills-morning",
            legacy_lookup={
                "category": categories["life-skills"],
                "shift": shifts["morning"],
            },
            defaults={
                "name": "Life Skills Weekday Schedule",
                "category": categories["life-skills"],
                "shift": shifts["morning"],
                "sort_order": 10,
                "is_active": True,
            },
        )
        life_section = self._upsert_seeded(
            ChecklistSection,
            seed_key="section-life-skills-morning-01",
            legacy_lookup={"definition": life_definition, "sort_order": 10},
            defaults={
                "definition": life_definition,
                "name": "Weekday Schedule",
                "sort_order": 10,
                "is_active": True,
            },
        )
        self._retire_removed_life_skills(life_section)
        active_life_skill_keys = []
        for order, (slot_key, start, end, labels) in enumerate(
            LIFE_SKILLS_SLOTS, start=1
        ):
            for weekday, label in enumerate(labels):
                task_key = f"task-life-skills-{slot_key}-{weekday}"
                active_life_skill_keys.append(task_key)
                self._upsert_seeded(
                    TaskDefinition,
                    seed_key=task_key,
                    legacy_lookup={
                        "section": life_section,
                        "weekday": weekday,
                        "scheduled_start": parse_time(start),
                        "scheduled_end": parse_time(end),
                    },
                    defaults={
                        "section": life_section,
                        "weekday": weekday,
                        "sort_order": order * 10,
                        "label": label,
                        "allow_na": False,
                        "is_active": True,
                        "scheduled_start": parse_time(start),
                        "scheduled_end": parse_time(end),
                    },
                )
        life_section.tasks.filter(seed_key__startswith="task-life-skills-").exclude(
            seed_key__in=active_life_skill_keys
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
            is_staff=False,
            is_superuser=False,
            password_env="CHORE_OPERATIONAL_PASSWORD",
            reset_passwords=options["reset_passwords"],
            operational_program=program,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded Sonder House operational access, 30 staff, 4 categories, "
                "3 shifts, 7 checklists, and 190 active tasks (40 Life Skills)."
            )
        )
        if (
            not os.environ.get("CHORE_OPERATIONAL_PASSWORD")
            and not operational_user.has_usable_password()
        ):
            self.stdout.write(
                self.style.WARNING(
                    "The Sonder House operational account has no usable password. Set "
                    "CHORE_OPERATIONAL_PASSWORD and rerun "
                    "with --reset-passwords."
                )
            )

    def _seed_standard_sections(
        self, definition, category_key, shift_key, sections
    ):
        for section_order, (section_name, labels) in enumerate(sections, start=1):
            section_key = (
                f"section-{category_key}-{shift_key}-{section_order:02d}"
            )
            section = self._upsert_seeded(
                ChecklistSection,
                seed_key=section_key,
                legacy_lookup={
                    "definition": definition,
                    "sort_order": section_order * 10,
                },
                defaults={
                    "definition": definition,
                    "name": section_name,
                    "sort_order": section_order * 10,
                    "is_active": True,
                },
            )
            active_task_keys = []
            for task_order, label in enumerate(labels, start=1):
                task_key = f"task-{category_key}-{shift_key}-{section_order:02d}-{task_order:03d}"
                active_task_keys.append(task_key)
                self._upsert_seeded(
                    TaskDefinition,
                    seed_key=task_key,
                    legacy_lookup={
                        "section": section,
                        "weekday": None,
                        "sort_order": task_order * 10,
                    },
                    defaults={
                        "section": section,
                        "weekday": None,
                        "sort_order": task_order * 10,
                        "label": label,
                        "allow_na": label in ALLOW_NA_LABELS,
                        "is_active": True,
                        "scheduled_start": None,
                        "scheduled_end": None,
                    },
                )
            section.tasks.filter(seed_key__startswith=f"task-{category_key}-{shift_key}-").exclude(
                seed_key__in=active_task_keys
            ).update(is_active=False)

    def _retire_removed_life_skills(self, life_section):
        for retired_order, (slot_key, start, end) in enumerate(
            RETIRED_LIFE_SKILLS_SLOTS, start=1
        ):
            for weekday in range(5):
                task_key = f"task-life-skills-retired-{slot_key}-{weekday}"
                task = TaskDefinition.objects.filter(seed_key=task_key).first()
                if task is None:
                    task = TaskDefinition.objects.filter(
                        seed_key__isnull=True,
                        section=life_section,
                        weekday=weekday,
                        scheduled_start=parse_time(start),
                        scheduled_end=parse_time(end),
                    ).first()
                if task is not None:
                    task.seed_key = task_key
                    task.is_active = False
                    task.sort_order = 1000 + (retired_order * 10)
                    task.save(update_fields=("seed_key", "is_active", "sort_order"))

    def _upsert_seeded(self, model, *, seed_key, legacy_lookup, defaults):
        instance = model.objects.filter(seed_key=seed_key).first()
        if instance is None:
            instance = model.objects.filter(
                seed_key__isnull=True, **legacy_lookup
            ).first()
        if instance is None:
            return model.objects.create(seed_key=seed_key, **defaults)

        instance.seed_key = seed_key
        for field, value in defaults.items():
            setattr(instance, field, value)
        instance.save()
        return instance

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
        operational_program=None,
    ):
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = True
        user.is_staff = is_staff
        user.is_superuser = is_superuser
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
