from django.db import migrations


ORDER_BY_SEED_KEY = {
    "definition-front-desk-morning": 10,
    "definition-front-desk-evening": 20,
    "definition-front-desk-night": 30,
    "definition-support-morning": 40,
    "definition-support-evening": 50,
    "definition-awake-night-night": 60,
    "definition-life-skills-morning": 70,
}

PREVIOUS_ORDER_BY_SEED_KEY = {
    "definition-front-desk-morning": 10,
    "definition-front-desk-evening": 20,
    "definition-front-desk-night": 30,
    "definition-support-morning": 10,
    "definition-support-evening": 20,
    "definition-awake-night-night": 10,
    "definition-life-skills-morning": 10,
}


def set_seeded_assignment_order(apps, schema_editor, *, ordering):
    ChecklistDefinition = apps.get_model("checklists", "ChecklistDefinition")
    for seed_key, sort_order in ordering.items():
        ChecklistDefinition.objects.filter(seed_key=seed_key).update(sort_order=sort_order)


def apply_order(apps, schema_editor):
    set_seeded_assignment_order(apps, schema_editor, ordering=ORDER_BY_SEED_KEY)


def reverse_order(apps, schema_editor):
    set_seeded_assignment_order(apps, schema_editor, ordering=PREVIOUS_ORDER_BY_SEED_KEY)


class Migration(migrations.Migration):
    dependencies = [("checklists", "0007_assignmentsectionmembership_discrepancyexplanation_and_more")]

    operations = [migrations.RunPython(apply_order, reverse_order)]
