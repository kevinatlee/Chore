from django.db import migrations


TARGETS = (
    (
        "phase5-task-0ea65e10607f1b3945a0",
        "Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list",
    ),
    ("phase5-task-03a8dcc33316f2ca4e41", "Review Heat Hut"),
    ("phase5-task-ca72368919b93134d763", "Check and reply to emails"),
    (
        "phase5-task-08179a09b70ca6748b49",
        "Complete leave requests and maintenance requests",
    ),
    ("phase5-task-e07e816fcb3b44ca0aad", "Submit leave requests"),
    ("phase5-task-856c28710c15e5859166", "Complete Citation training"),
)


def activate_and_backfill(apps, schema_editor):
    TaskDefinition = apps.get_model("checklists", "TaskDefinition")
    ChecklistItem = apps.get_model("checklists", "ChecklistItem")
    ActiveTaskCompletion = apps.get_model("checklists", "ActiveTaskCompletion")

    if TaskDefinition.objects.filter(seed_key__startswith="phase5-task-").exists():
        matched_ids = []
        missing = []
        for seed_key, label in TARGETS:
            matches = list(
                TaskDefinition.objects.filter(seed_key=seed_key, label=label).values_list(
                    "pk", flat=True
                )
            )
            if len(matches) != 1:
                missing.append(f"{seed_key}: {label}")
            else:
                matched_ids.append(matches[0])
        if missing:
            raise RuntimeError(
                "Refusing ambiguous multi-staff task activation; exact targets missing: "
                + "; ".join(missing)
            )
        TaskDefinition.objects.filter(pk__in=matched_ids).update(
            required_staff_completions=2
        )

    for item in ChecklistItem.objects.filter(
        current_state="completed", current_staff__isnull=False
    ).iterator():
        ActiveTaskCompletion.objects.get_or_create(
            item_id=item.pk,
            staff_id=item.current_staff_id,
            is_active=True,
            defaults={"completed_at": item.state_changed_at or item.updated_at},
        )


class Migration(migrations.Migration):
    dependencies = [("checklists", "0010_feedback_and_staff_completions")]

    operations = [migrations.RunPython(activate_and_backfill, migrations.RunPython.noop)]

