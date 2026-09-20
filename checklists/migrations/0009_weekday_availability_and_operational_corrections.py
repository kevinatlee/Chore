import hashlib

from django.db import migrations, models


LABEL_CHANGES = {
    "Complete leave requests and maintenance requests as needed": (
        "Complete leave requests and maintenance requests",
        True,
    ),
    "Photocopy and maintain an ample supply of paperwork": (
        "Print and maintain an ample supply of paperwork",
        True,
    ),
    "Complete Citation training as required": ("Complete Citation training", True),
    "Roll coins as needed": ("Roll coins", True),
    "Shovel / salt front steps and walkway as needed": (
        "Shovel / salt front steps and walkway",
        True,
    ),
    "Complete Incident Reports as needed": ("Complete Incident Reports", True),
    "Water plants as needed": ("Water plants", True),
    "Submit leave requests as needed": ("Submit leave requests", True),
    "Update forms as needed": ("Update forms", True),
}

HALLWAY_TASKS = [
    "Sweep / mop first floor hallway",
    "Sweep / mop second floor hallway",
    "Sweep / mop third floor hallway",
    "Wipe first floor window ledges",
    "Wipe second floor window ledges",
    "Wipe third floor window ledges",
]

REMOVED_ACTIVE_TASK_LABELS = ("Print needed forms",)


def stable_key(prefix, value):
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"phase5-{prefix}-{digest}"


def section_key(section_name, task_specs):
    identity = section_name + "\n" + "\n".join(
        f"{label}|{int(allow_na)}" for label, allow_na in task_specs
    )
    return stable_key("section", identity)


def apply_corrections(apps, schema_editor):
    ChecklistDefinition = apps.get_model("checklists", "ChecklistDefinition")
    ChecklistSection = apps.get_model("checklists", "ChecklistSection")
    TaskDefinition = apps.get_model("checklists", "TaskDefinition")
    AssignmentSectionMembership = apps.get_model(
        "checklists", "AssignmentSectionMembership"
    )
    SectionTaskMembership = apps.get_model("checklists", "SectionTaskMembership")

    ChecklistDefinition.objects.filter(
        seed_key="definition-life-skills-morning"
    ).update(weekdays_only=True)

    for old_label, (new_label, allow_na) in LABEL_CHANGES.items():
        task = TaskDefinition.objects.filter(
            seed_key=stable_key("task", old_label)
        ).first()
        if task is None:
            continue
        task.seed_key = stable_key("task", new_label)
        task.label = new_label
        task.allow_na = allow_na
        task.is_active = True
        task.save(update_fields=("seed_key", "label", "allow_na", "is_active"))

    greet_task = TaskDefinition.objects.filter(
        seed_key=stable_key("task", "Greet / buzz in tenants")
    ).first()
    if greet_task is not None:
        SectionTaskMembership.objects.filter(
            task=greet_task,
            section__assignment_memberships__assignment__seed_key__in=(
                "definition-front-desk-morning",
                "definition-front-desk-evening",
            ),
        ).delete()
        still_active = SectionTaskMembership.objects.filter(
            task=greet_task,
            section__is_active=True,
            section__assignment_memberships__assignment__is_active=True,
        ).exists()
        if not still_active:
            greet_task.is_active = False
            greet_task.save(update_fields=("is_active",))

    for label in REMOVED_ACTIVE_TASK_LABELS:
        task = TaskDefinition.objects.filter(
            seed_key=stable_key("task", label)
        ).first()
        if task is None:
            continue
        SectionTaskMembership.objects.filter(
            task=task,
            section__is_active=True,
            section__assignment_memberships__assignment__is_active=True,
        ).delete()
        still_active = SectionTaskMembership.objects.filter(
            task=task,
            section__is_active=True,
            section__assignment_memberships__assignment__is_active=True,
        ).exists()
        if not still_active:
            task.is_active = False
            task.save(update_fields=("is_active",))

    # Section keys include ordered task labels and N/A eligibility. Rewrite the
    # existing seed-managed records in place so the next seed run reuses them.
    for section in ChecklistSection.objects.filter(
        seed_key__startswith="phase5-section-", is_active=True
    ):
        specs = list(
            SectionTaskMembership.objects.filter(section=section)
            .select_related("task")
            .order_by("sort_order", "id")
            .values_list("task__label", "task__allow_na")
        )
        section.seed_key = section_key(section.name, specs)
        section.save(update_fields=("seed_key",))

    awake_assignment = ChecklistDefinition.objects.filter(
        seed_key="definition-awake-night-night"
    ).first()
    if awake_assignment is None:
        return
    awake_hallways_membership = AssignmentSectionMembership.objects.filter(
        assignment=awake_assignment, section__name="Hallways"
    ).select_related("section").first()
    if awake_hallways_membership is None:
        return

    tasks = []
    for label in HALLWAY_TASKS:
        task, _ = TaskDefinition.objects.update_or_create(
            seed_key=stable_key("task", label),
            defaults={
                "label": label,
                "allow_na": False,
                "requires_completion_note": False,
                "is_active": True,
                "scheduled_start": None,
                "scheduled_end": None,
            },
        )
        tasks.append(task)

    new_section, _ = ChecklistSection.objects.update_or_create(
        seed_key=section_key("Hallways", [(task.label, False) for task in tasks]),
        defaults={
            "name": "Hallways",
            "sort_order": awake_hallways_membership.section.sort_order,
            "is_active": True,
        },
    )
    for index, task in enumerate(tasks, start=1):
        SectionTaskMembership.objects.update_or_create(
            section=new_section,
            task=task,
            defaults={"sort_order": index * 10},
        )
    new_section.task_memberships.exclude(task_id__in=[task.pk for task in tasks]).delete()
    awake_hallways_membership.section = new_section
    awake_hallways_membership.save(update_fields=("section",))


class Migration(migrations.Migration):
    dependencies = [
        ("checklists", "0008_normalize_seeded_shift_assignment_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklistdefinition",
            name="weekdays_only",
            field=models.BooleanField(
                default=False,
                help_text="Prevent operational use on Saturday and Sunday.",
                verbose_name="Monday–Friday only",
            ),
        ),
        migrations.AlterModelOptions(
            name="discrepancyexplanation",
            options={
                "ordering": ("created_at", "id"),
                "verbose_name": "comments for incomplete tasks",
                "verbose_name_plural": "comments for incomplete tasks",
            },
        ),
        migrations.RunPython(apply_corrections, migrations.RunPython.noop),
    ]
