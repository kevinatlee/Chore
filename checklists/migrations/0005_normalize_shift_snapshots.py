from datetime import time

from django.db import migrations


def normalize_shift_snapshots(apps, schema_editor):
    ChecklistInstance = apps.get_model("checklists", "ChecklistInstance")
    boundaries = {
        "Morning": (time(7), time(15)),
        "Evening": (time(15), time(23)),
        "Night": (time(23), time(7)),
    }
    for name, (start, end) in boundaries.items():
        ChecklistInstance.objects.filter(
            shift__name=name,
        ).update(
            shift_name_snapshot=name,
            shift_start_snapshot=start,
            shift_end_snapshot=end,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("checklists", "0004_operational_staff_and_shift_corrections"),
    ]

    operations = [
        migrations.RunPython(normalize_shift_snapshots, migrations.RunPython.noop),
    ]
