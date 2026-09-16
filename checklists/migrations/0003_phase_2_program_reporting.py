import checklists.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def assign_existing_program(apps, schema_editor):
    Program = apps.get_model("checklists", "Program")
    ProgramMembership = apps.get_model("checklists", "ProgramMembership")
    StaffCategory = apps.get_model("checklists", "StaffCategory")
    ChecklistInstance = apps.get_model("checklists", "ChecklistInstance")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    program, _ = Program.objects.get_or_create(
        slug="sonder-house", defaults={"name": "Sonder House", "is_active": True}
    )
    StaffCategory.objects.filter(program__isnull=True).update(program=program)
    ChecklistInstance.objects.filter(program__isnull=True).update(program=program)

    test_staff = User.objects.filter(username="teststaff").first()
    if test_staff:
        ProgramMembership.objects.get_or_create(
            user=test_staff,
            program=program,
            defaults={"role": "staff", "is_active": True, "is_test_staff": True},
        )
    for user in User.objects.filter(is_staff=True, is_superuser=False):
        ProgramMembership.objects.get_or_create(
            user=user,
            program=program,
            defaults={"role": "manager", "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("checklists", "0002_add_seed_keys"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Program",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("slug", models.SlugField(max_length=80, unique=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ("name", "id")},
        ),
        migrations.AddField(
            model_name="staffcategory",
            name="program",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="staff_categories", to="checklists.program"),
        ),
        migrations.AddField(
            model_name="checklistinstance",
            name="program",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="checklist_instances", to="checklists.program"),
        ),
        migrations.AlterField(
            model_name="staffcategory",
            name="slug",
            field=models.SlugField(max_length=80),
        ),
        migrations.AddConstraint(
            model_name="staffcategory",
            constraint=models.UniqueConstraint(fields=("program", "slug"), name="unique_program_category_slug"),
        ),
        migrations.CreateModel(
            name="ProgramMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("staff", "Staff"), ("manager", "Manager")], max_length=16)),
                ("is_active", models.BooleanField(default=True)),
                ("is_test_staff", models.BooleanField(default=False)),
                ("receive_scheduled_reports", models.BooleanField(default=False)),
                ("program", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="memberships", to="checklists.program")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="program_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("program__name", "role", "user__username"),
                "constraints": [models.UniqueConstraint(fields=("user", "program"), name="unique_user_program_membership")],
            },
        ),
        migrations.CreateModel(
            name="ScheduledReportDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cadence", models.CharField(choices=[("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly"), ("annual", "Annual")], max_length=16)),
                ("period_start", models.DateField()),
                ("period_end", models.DateField()),
                ("recipient_email", models.EmailField(max_length=254)),
                ("generated_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("state", models.CharField(choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed")], default="pending", max_length=16)),
                ("subject", models.CharField(max_length=255)),
                ("body_html", models.TextField()),
                ("snapshot", models.JSONField(default=dict)),
                ("error_message", models.TextField(blank=True)),
                ("program", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="report_deliveries", to="checklists.program")),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="scheduled_report_deliveries", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-period_end", "program__name", "cadence", "recipient_email"),
                "constraints": [models.UniqueConstraint(fields=("program", "cadence", "period_start", "period_end", "recipient"), name="unique_scheduled_report_delivery")],
            },
        ),
        migrations.RunPython(assign_existing_program, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="staffcategory",
            name="program",
            field=models.ForeignKey(default=checklists.models.get_default_program_pk, on_delete=django.db.models.deletion.PROTECT, related_name="staff_categories", to="checklists.program"),
        ),
        migrations.AlterField(
            model_name="checklistinstance",
            name="program",
            field=models.ForeignKey(default=checklists.models.get_default_program_pk, on_delete=django.db.models.deletion.PROTECT, related_name="checklist_instances", to="checklists.program"),
        ),
    ]
